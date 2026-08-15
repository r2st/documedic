"""Making a handler take the same time whichever branch it took.

An endpoint that answers identically for two cases has still told them apart if it answers one
of them faster. The password-reset routes are written to be indistinguishable — same 202, same
body, same 401, one message for all four ways a token can be bad — and every one of those
promises is made in the *response*. None of them is made in the clock.

``app.services.auth_service.login`` already handles its half of this a different way: it runs a
bcrypt verification against a dummy hash so the unknown-email branch burns the same CPU as the
real one. That works there because the asymmetry is a single expensive call. It does not
generalise to the reset routes, where the branches differ by five or six database round-trips,
an insert, an audit row and a commit — there is no dummy account to write those against, and
faking them would mean writing rows for addresses that have no account.

So the reset routes take the other standard approach: a floor on the response time, set well
above what either branch costs, applied to both. Padding is only ever added, never removed, so
this cannot make a slow request look fast; and when real work exceeds the floor the difference
returns, which is why the floor is chosen with headroom rather than trimmed to the measured
cost.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def with_minimum_duration[T](
    seconds: float, work: Callable[[], Awaitable[T]], *, label: str
) -> T:
    """Run ``work``, and do not let the caller have the result before ``seconds`` have passed.

    The padding is in a ``finally``, which is the whole point rather than an implementation
    detail: on both routes this protects, the *fast* branch is the one that raises. An unknown
    reset token raises :class:`~app.exceptions.InvalidResetTokenError` after a single indexed
    SELECT, while the valid one goes on to hash a password and revoke every session. Padding
    only the returning branch would leave the raising one unpadded and the endpoint exactly as
    readable from the outside as before.

    ``asyncio.sleep`` rather than a busy wait: the point is to withhold the response, not to
    occupy the event loop, and these are unauthenticated routes an attacker can call in bulk.
    Both are rate-limited per address, so the held time is bounded by that rather than by this.

    A non-positive ``seconds`` disables the floor and runs ``work`` unchanged, which is how a
    test that is measuring something else opts out.
    """
    if seconds <= 0:
        return await work()
    start = time.perf_counter()
    try:
        return await work()
    finally:
        remaining = seconds - (time.perf_counter() - start)
        if remaining > 0:
            await asyncio.sleep(remaining)
        else:
            # The floor stopped covering this route. Not an error — the response is still
            # correct — but the anti-enumeration property it exists for is gone until the floor
            # is raised, and nothing else would ever say so.
            logger.warning(
                "%s took %.3fs, at or beyond its %.3fs constant-time floor; the floor no "
                "longer hides which branch it took.",
                label,
                time.perf_counter() - start,
                seconds,
            )
