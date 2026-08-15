"""A client-supplied ``X-Request-Id`` must not be adopted verbatim.

The correlation id is convenient precisely because it goes everywhere: it is echoed in the
response header, interpolated into the 500 body's clinician-facing prose, and written into every
log line the request produces. Taken straight off the wire, all three of those took whatever the
caller sent — four kilobytes of it, an ANSI escape sequence, a fragment of markup.

The log is the one that matters. Its readers are operators in a terminal during an incident,
and ``\\x1b[2J`` clears their screen; a value with an embedded control character corrupts the
line an investigation is being read from. There was nothing making this safe: h11 rejects CR and
LF in a header value and nothing else, so every other byte arrived intact — which is what the
first two tests here demonstrate, because "surely the framework strips that" is the assumption
this whole file exists to disprove.

Adopting a well-formed client id stays deliberate: it is how a request is followed across the
proxy and the frontend, and the OpenAPI description promises it.
"""

from __future__ import annotations

import uuid

import pytest

from app.middleware import resolve_request_id

PATIENTS = "/api/v1/patients"


def _is_generated(value: str) -> bool:
    """True when the id looks like one we minted rather than one we were handed."""
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# --- What actually reaches us --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "supplied"),
    [
        ("an ANSI escape that clears an operator's terminal", "\x1b[2Jcleared"),
        ("an embedded control character", "abc\x07def"),
        ("a tab, which splits a log line into columns", "abc\tdef"),
        ("four kilobytes of padding", "A" * 4096),
        ("markup, for whatever renders the 500 body", "<script>alert(1)</script>"),
        ("whitespace and punctuation a log parser splits on", 'id with spaces, "quotes"'),
        ("an empty value", ""),
    ],
)
async def test_a_malformed_request_id_is_replaced_not_echoed(client, label, supplied):
    """Whatever was sent, what comes back is one we generated. No sanitising in place —
    substitution, so there is no partially-cleaned value to reason about."""
    resp = await client.get(PATIENTS, headers={"X-Request-Id": supplied})

    echoed = resp.headers["X-Request-Id"]
    assert echoed != supplied, f"{label} was reflected back verbatim"
    assert _is_generated(echoed), f"{label} produced {echoed!r}, which is not a generated id"


@pytest.mark.asyncio
async def test_the_reflected_id_is_bounded(client):
    """The header is echoed on every response, so an unbounded value is an amplifier."""
    resp = await client.get(PATIENTS, headers={"X-Request-Id": "A" * 4096})

    assert len(resp.headers["X-Request-Id"]) <= 128


@pytest.mark.asyncio
async def test_a_well_formed_client_id_is_still_honoured(client):
    """The feature this guards is not the feature it removes.

    Correlating a request across the frontend, the proxy and this service depends on the
    client's id being adopted, and the OpenAPI description promises it is.
    """
    supplied = "0af7651916cd43dd8448eb211c80319c"

    resp = await client.get(PATIENTS, headers={"X-Request-Id": supplied})

    assert resp.headers["X-Request-Id"] == supplied


@pytest.mark.asyncio
async def test_a_request_with_no_id_still_gets_one(client):
    """Correlation is not optional just because the caller did not ask for it."""
    resp = await client.get(PATIENTS)

    assert _is_generated(resp.headers["X-Request-Id"])


# --- The 500 body, which is the other place the value lands --------------------------------


@pytest.mark.asyncio
async def test_a_rejected_id_does_not_reach_the_error_body(client, monkeypatch):
    """``internal_error_response`` puts the id in prose a clinician reads back out loud.

    That prose is also what a frontend error reporter ships onward, so it is the second place
    an unfiltered value travels to — and unlike the header, it is rendered rather than parsed.
    """
    from app.routers import health

    def boom(*_args, **_kwargs):
        raise RuntimeError("upstream fell over")

    # Patched at a call site rather than at the route function: FastAPI captured the handler
    # when the router was built, so replacing the module attribute would not be reached.
    monkeypatch.setattr(health, "text", boom)

    resp = await client.get("/health/ready", headers={"X-Request-Id": "<script>alert(1)</script>"})

    assert resp.status_code == 500
    body = resp.json()
    assert "<script>" not in body["message"]
    assert "<script>" not in (body["request_id"] or "")
    assert _is_generated(body["request_id"])


# --- The rule itself -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "supplied",
    [
        "0af7651916cd43dd8448eb211c80319c",  # a bare trace id
        "b7ad6b7169203331",  # a span id
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",  # W3C traceparent
        str(uuid.uuid4()),  # what we generate ourselves, with hyphens
        "req_01HQ3M8Z9K",  # underscore-prefixed, as several gateways emit
        "abc.def~ghi:jkl@mno+pqr/stu=",  # every punctuation mark the rule allows
        "A" * 128,  # exactly at the ceiling
    ],
)
def test_real_correlation_ids_are_accepted(supplied):
    """The character set has to admit the ids that are actually in use, or the guard silently
    turns off correlation for whatever emits them."""
    assert resolve_request_id(supplied) == supplied


@pytest.mark.parametrize(
    "supplied",
    [
        None,
        "",
        "A" * 129,  # one past the ceiling
        "has space",
        "has\ttab",
        "has\x1b[0mescape",
        "has\x00null",
        "<angle>",
        'has"quote',
        "has;semicolon",
        "has,comma",
        "unicode—dash",
    ],
)
def test_everything_else_gets_a_generated_id(supplied):
    assert _is_generated(resolve_request_id(supplied))


def test_the_substitution_is_logged_without_the_value(caplog):
    """The reason for replacing a value is never a reason to write it down.

    Logging the rejected id to explain that it was unsafe to log would put it in the log
    anyway — with the escape sequence intact and an operator reading it.
    """
    with caplog.at_level("INFO"):
        resolve_request_id("\x1b[2Jmalicious-value-here")

    messages = [record.getMessage() for record in caplog.records]
    assert any("malformed X-Request-Id" in message for message in messages)
    assert not any("malicious-value-here" in message for message in messages)
    assert not any("\x1b" in message for message in messages)
