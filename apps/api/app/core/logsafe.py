"""Naming a failure for a log line without echoing whatever the failure is carrying.

``str(exc)`` is the reflex when something goes wrong, and on the paths that talk to an LLM
provider or a document extractor it is the wrong one. Those calls send the patient snapshot
upstream, and an error raised in response can quote it straight back:

  * OpenRouter — the primary provider — returns moderation refusals with a ``flagged_input``
    field holding the offending text. That text is the prompt, which is the chart.
  * Provider SDKs render ``str(exc)`` as ``Error code: 400 - {...response body...}``, so
    anything the upstream chose to echo is echoed again into wherever it is written.
  * A database error's message quotes the row that violated the constraint.

``app.main.unhandled_error_handler`` already refuses to put exception text in a response body
for exactly this reason. This is the same rule for the places that are not an HTTP response:
log lines, ``audit_logs.payload`` (unencrypted, immutable, never pruned), persisted
``error_detail`` columns, and the SSE stream.

What survives is what an operator actually triages on — which exception, and the upstream
status code when there is one. A message is included only when its class opts in with
``log_safe_message = True``, which means "this codebase authored this string". An exception
that wraps a third-party error's text into its own message is not log-safe however it is
declared, so those wrap sites pass the value through this function first.
"""

from __future__ import annotations


def describe_exception(exc: BaseException | None) -> str:
    """Render ``exc`` as a log-safe string: type name, and an upstream status code if present.

    >>> describe_exception(ValueError("patient Asha Reddy, dob 1979-02-11"))
    'ValueError'
    """
    if exc is None:
        return "unknown"

    name = type(exc).__name__

    # Provider SDKs (openai, anthropic, httpx) all expose the upstream status this way. It is
    # the single most useful field for triage — 429 and 500 mean entirely different things —
    # and it is a number, so it cannot be carrying a chart.
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        status = None

    if getattr(type(exc), "log_safe_message", False):
        message = str(exc).strip()
        if message:
            name = f"{name}: {message}"

    return f"{name} (HTTP {status})" if status is not None else name
