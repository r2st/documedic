"""The correlation id, carried out of the request scope so every log line can find it.

``RequestContextMiddleware`` has assigned each request an id for a long time, and put it on
``request.state``. That reaches exactly the code holding a ``Request``: three exception handlers
in ``app.main``, which interpolate it by hand. Nothing else in the process could see it — not
``SafetyService``, not an agent node, not the extraction pipeline, not the SSE worker — so the
log lines that describe what actually went wrong were the ones with no id on them, and an
operator holding the reference a clinician read off a 500 page could find only the handler line
that produced it.

A :class:`~contextvars.ContextVar` is the right shape for this because the propagation rules
already match how the request's work spreads out:

* ``asyncio.create_task`` copies the current context, so the reasoning SSE worker inherits it.
* ``asyncio.to_thread`` copies it too, so blob I/O, bcrypt and the embedder stay correlated.
* ``run_in_executor`` does **not**, which is why ``agents.util.call_llm`` and the health probe
  copy the context by hand before handing work to the LLM pool. ``call_llm`` has done that
  since the pool was introduced, with a comment naming "the request id the logs are correlated
  by" — for a variable that did not exist yet. This is that variable.

Nothing reads the value directly except :mod:`app.core.logging_config`, which stamps it onto
every record. Call sites do not mention the id at all; that is the point.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

# The default is deliberately None rather than a generated id: work that is genuinely not part
# of a request (startup seeding, a cron-style sweep) should be recognisable as such in the log
# rather than wearing a correlation id that correlates with nothing.
_request_id: ContextVar[str | None] = ContextVar("aether_request_id", default=None)

# What the formatter prints for a record emitted outside any request.
NO_REQUEST_ID = "-"


def current_request_id() -> str | None:
    """This context's correlation id, or None outside a request."""
    return _request_id.get()


def bind_request_id(request_id: str | None) -> Token[str | None]:
    """Make ``request_id`` the correlation id for this context. Returns a token to reset with."""
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Undo a :func:`bind_request_id`.

    Worth doing even though a server request context is discarded when the request ends: the
    same context is reused for the whole life of a worker *thread*, so a value set inside one
    and never reset outlives the call that set it and mislabels the next one's log lines.
    """
    _request_id.reset(token)
