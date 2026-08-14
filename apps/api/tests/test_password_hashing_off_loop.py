"""bcrypt must not run on the event loop.

bcrypt is deliberately slow, and it is slow by burning CPU rather than by waiting on anything.
Called straight from an async handler it stops the loop dead: measured at the configured 12
rounds, ~265ms for a hash and ~380ms for a verification, during which every other request this
worker is serving makes no progress at all -- including the SSE streams carrying live reasoning
output to a clinician mid-consultation.

The login path made that worse than one stall per sign-in. Its account-enumeration defence runs a
verification against a dummy hash even when the email is unknown, on purpose, so unauthenticated
requests to nonexistent addresses each froze the worker for the better part of a second, one after
another -- a defence that doubled as a cheap way to stall the API.

These tests assert the property rather than a duration, because a duration is a flaky test on a
loaded machine: bcrypt has to end up on a thread that is not the loop's, and the loop has to stay
free while it does. The enumeration defence is asserted alongside it, since the whole point of
moving the work is that its cost is still paid.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.core.security import (
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)
from app.services import auth_service as auth_module
from app.services.auth_service import AuthService


@pytest.fixture
def bcrypt_threads(monkeypatch):
    """Record the thread every bcrypt call runs on, without slowing the tests down.

    Patched at ``bcrypt`` itself rather than at ``app.core.security``. That is the difference
    between a test of the fix and a test of an import statement: ``auth_service`` binds the
    helpers by name at import, so replacing ``app.core.security.verify_password`` would not be
    seen by a caller that had gone back to the synchronous one, and the test would "fail" for
    the wrong reason. Everything reaches bcrypt eventually, whichever wrapper it went through.

    The real primitives cost a quarter of a second a call, and what is under test is *where* the
    work happens, so paying that would make the suite worse for no extra coverage.
    """
    threads: list[str] = []

    def _hashpw(pw: bytes, salt: bytes) -> bytes:
        threads.append(threading.current_thread().name)
        return b"hashed:" + pw

    def _checkpw(pw: bytes, hashed: bytes) -> bool:
        threads.append(threading.current_thread().name)
        return hashed == b"hashed:" + pw

    monkeypatch.setattr("bcrypt.hashpw", _hashpw)
    monkeypatch.setattr("bcrypt.checkpw", _checkpw)
    return threads


# --- the wrappers themselves ----------------------------------------------------------------


async def test_the_async_hash_is_the_same_hash():
    """Off-loading may not change the value: existing stored hashes have to keep verifying."""
    hashed = await hash_password_async("correct horse battery staple")

    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong", hashed)


async def test_the_async_verify_agrees_with_the_synchronous_one():
    hashed = hash_password("s3cret-passphrase")

    assert await verify_password_async("s3cret-passphrase", hashed) is True
    assert await verify_password_async("s3cret-passphras", hashed) is False


async def test_the_async_verify_still_fails_closed_on_a_corrupt_stored_hash():
    """A bad row must be a failed sign-in, not a 500 -- unchanged by the off-load."""
    assert await verify_password_async("anything", "") is False
    assert await verify_password_async("anything", "not-a-bcrypt-hash") is False


async def test_hashing_leaves_the_event_loop_thread(bcrypt_threads):
    loop_thread = threading.current_thread().name

    await hash_password_async("pw")
    await verify_password_async("pw", "hashed:pw")

    assert bcrypt_threads, "bcrypt was never called"
    assert all(name != loop_thread for name in bcrypt_threads), bcrypt_threads


async def test_the_loop_keeps_running_while_a_password_is_hashed():
    """The point of the whole change, stated as behaviour rather than as a thread name.

    The hash below is a real one, so it genuinely occupies a core for a substantial time. If it
    ran on the loop, nothing else scheduled on the loop could advance until it finished.
    """
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    ticker = asyncio.create_task(tick())
    await hash_password_async("a real bcrypt hash, paid in full")
    ticker.cancel()

    assert ticks > 0, "the event loop made no progress while bcrypt ran"


# --- the callers ----------------------------------------------------------------------------


async def test_signup_does_not_hash_on_the_event_loop(db, bcrypt_threads):
    loop_thread = threading.current_thread().name

    await AuthService(db).signup("newdoc@example.com", "a-good-passphrase", "Dr New")

    assert bcrypt_threads, "signup did not hash a password at all"
    assert all(name != loop_thread for name in bcrypt_threads), bcrypt_threads


async def test_login_does_not_verify_on_the_event_loop(db, bcrypt_threads):
    service = AuthService(db)
    await service.signup("doc@example.com", "a-good-passphrase", "Dr Who")
    await db.commit()
    loop_thread = threading.current_thread().name
    bcrypt_threads.clear()

    await service.login("doc@example.com", "a-good-passphrase")

    assert bcrypt_threads, "login did not verify a password at all"
    assert all(name != loop_thread for name in bcrypt_threads), bcrypt_threads


async def test_an_unknown_email_still_pays_for_a_verification(db, bcrypt_threads):
    """The enumeration defence has to survive the move, or the move traded one bug for another.

    Its whole mechanism is spending the same CPU on a miss as on a hit. Moving that spend to a
    worker thread is fine; skipping it is not, because then "no such account" returns visibly
    faster than "wrong password" and the endpoint tells an attacker which addresses are real.
    """
    from app.exceptions import InvalidCredentialsError

    with pytest.raises(InvalidCredentialsError):
        await AuthService(db).login("nobody@example.com", "whatever")

    assert bcrypt_threads, "an unknown email skipped the dummy verification"


async def test_the_dummy_hash_is_a_real_hash_of_something_unguessable():
    """It is built at import time with the *synchronous* primitive, deliberately.

    There is no event loop yet when the module is imported, so there is nothing to keep clear of,
    and an ``asyncio.run`` at import would be a worse answer than a plain call.
    """
    assert auth_module._DUMMY_PASSWORD_HASH.startswith("$2")
    assert not verify_password("", auth_module._DUMMY_PASSWORD_HASH)
