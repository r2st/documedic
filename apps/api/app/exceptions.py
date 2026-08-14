"""Domain exceptions, mapped to HTTP responses by handlers in main.py.

Every ``message`` here reaches a clinician, usually as a toast or an inline banner in the
middle of a consultation, so each one is written to answer "what do I do now?" rather than to
describe the internal failure. Naming the mechanism ("wrong token type", "could not resolve via
DrugVocabulary") tells the person at the keyboard nothing they can act on and reads as a system
fault they must escalate.

Where the internal cause is still worth having, pass it as ``detail=``: it is logged against
the request id by ``main.aether_error_handler`` and never serialized into the response, so
operators keep the diagnosis without the clinician getting the jargon.
"""

from __future__ import annotations


def default_message(error_class: type[Exception]) -> str:
    """The clinician-facing message carried by an exception class's docstring.

    Only the docstring's **first paragraph** is the message; anything after a blank line is
    developer commentary (why the code is what it is) and stays out of the response body. The
    paragraph's line wrapping is collapsed so the message serializes as one line rather than
    carrying source indentation into the JSON.
    """
    doc = (error_class.__doc__ or "").split("\n\n", 1)[0]
    return " ".join(doc.split()) or "Error"


class AetherError(Exception):
    """Base class for all domain errors."""

    status_code: int = 400
    code: str = "error"

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        self.message = message or default_message(type(self))
        # Internal cause, for logs only. Never put patient data or secrets in it: it lands in
        # the application log, which is not a clinical record.
        self.detail = detail
        super().__init__(self.message)

    @property
    def headers(self) -> dict[str, str] | None:
        """Response headers this error must carry, serialized by ``main.aether_error_handler``.

        Empty for almost every error — the ``{code, message}`` body is the contract. It exists
        for the cases where the status code is only half the answer and the rest belongs in a
        header a client already knows how to read, ``Retry-After`` on a 429 being the one.
        """
        return None


class NotFoundError(AetherError):
    """That record was not found. It may have been removed, or the link may be out of date."""

    status_code = 404
    code = "not_found"


class PatientNotFoundError(NotFoundError):
    """No patient chart matches this link. Search the patient list to reopen the chart."""

    code = "patient_not_found"


class DocumentNotFoundError(NotFoundError):
    """This document is no longer in the chart. Upload the file again if it is still needed."""

    code = "document_not_found"


class PathwayNotFoundError(NotFoundError):
    """No curated clinical pathway covers this condition yet — the guideline corpus has none
    for it. The differential and drug-safety checks are unaffected."""

    code = "pathway_not_found"


class AuthError(AetherError):
    status_code = 401
    code = "unauthorized"


class InvalidCredentialsError(AuthError):
    """That email and password combination was not recognised. Check both and try again."""

    code = "invalid_credentials"


class TokenError(AuthError):
    """Your sign-in session is no longer valid. Sign in again to continue."""

    code = "invalid_token"


class SessionExpiredError(TokenError):
    """Your session ended after a period of inactivity. Sign in again to continue —
    nothing you saved to a chart was lost.

    Keeps TokenError's ``invalid_token`` code: the wire contract stays one code for every
    rejected token, and only the idle case (which already reveals itself by revoking the
    session and writing its own audit entry) says more in the message.
    """


class ForbiddenError(AetherError):
    """This account does not have access to that record."""

    status_code = 403
    code = "forbidden"


class TooManyAttemptsError(AetherError):
    """Too many failed attempts. Try again later."""

    status_code = 429
    code = "too_many_attempts"


class RateLimitExceededError(AetherError):
    """This is being asked for faster than the system will run it. Wait a moment and try again —
    nothing was saved to the chart, and the drug-safety checks are unaffected.

    Distinct from ``TooManyAttemptsError`` despite sharing the 429: that one is a security
    control against credential guessing and locks an account for minutes, this one is a capacity
    control on work that costs an LLM call and clears in seconds. A client that cannot tell them
    apart cannot decide between backing off and asking the clinician to sign in again, so they
    carry different codes.

    The second sentence is not filler. A clinician who hits this mid-consultation needs to know
    the two things a bare "too many requests" leaves open: whether a half-written record was
    left in the chart, and whether the deterministic allergy/interaction checks are still
    answering. Both answers are reassuring, and neither is guessable from the status code.
    """

    status_code = 429
    code = "rate_limited"

    def __init__(
        self,
        retry_after: int,
        message: str | None = None,
        *,
        detail: str | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(message, detail=detail)

    @property
    def headers(self) -> dict[str, str]:
        """``Retry-After``, so a client can back off correctly without parsing the prose.

        The wait is also stated in ``message`` for the human, but no client should scrape it
        from there — that text is clinician-facing and gets rewritten.
        """
        return {"Retry-After": str(self.retry_after)}


class ConflictError(AetherError):
    """That change conflicts with the current state of the record. Reload and try again."""

    status_code = 409
    code = "conflict"


class ConcurrentApprovalError(ConflictError):
    """Someone approved this document at the same moment, so nothing was merged twice. Reload
    the chart — if the report is not on it, approve again.

    Raised when the merge's INSERT is refused by ``uq_lab_results_observation``, which is the
    database refusing to record one blood draw as two. That only happens when two approvals of
    the same document overlap in the read-then-insert window that ``GraphService`` deduplicates
    in; the loser's whole transaction rolls back, so the record is left exactly as the winner
    wrote it and a retry is safe.

    409 rather than a silent 200 because the two are not the same answer. The caller asked for a
    merge and did not get one, and while the winning approval almost always merged the identical
    payload, it need not have: the loser may have carried a correction the winner did not.
    Saying so lets a client that retries do it against the current chart.
    """

    code = "concurrent_approval"


class ReasoningRunInProgressError(ConflictError):
    """This case is already being reasoned about. Watch the run that is going, or wait for it to
    finish and start another — nothing was lost, and the deterministic drug-safety checks are
    unaffected.

    Raised when the single-run claim in ``ReasoningService.run`` loses its compare-and-swap,
    which means another request took the session first. Two runs on one session are not two
    opinions the clinician can compare: each writes its own full set of ClinicalSuggestion rows
    against the same session_id, those rows are immutable by database trigger and cannot be
    cleaned up, and the session header (status, autonomy_tier, case_state) is last-write-wins.
    So the list would show every hypothesis twice while the header described only one of the two
    runs — including, if the runs disagreed, a header reading ``suggestive`` above a hard-blocked
    suggestion the other run produced.

    409 rather than quietly joining the run in progress: the caller asked for a run and did not
    get one. Retrying once the session leaves ``reasoning`` is safe and is the way to get output
    that reflects a chart that has since changed.
    """

    code = "reasoning_in_progress"


class EmailAlreadyExistsError(ConflictError):
    """An account with this email already exists. Sign in instead, or use another address."""

    code = "email_exists"


class ConsentRequiredError(AetherError):
    """Recorded patient consent is required before any clinical data can be stored (DPDP Act).
    Confirm consent with the patient, then tick the consent box to create the chart."""

    status_code = 422
    code = "consent_required"


class ConsentWithdrawnError(AetherError):
    """This patient has withdrawn consent, so new data cannot be added and the reasoning
    engine cannot be run on their record. The existing chart stays readable. Re-record consent
    on the patient's details before continuing.

    Withdrawal is a data principal's right under the DPDP Act 2023, and it has to stop
    processing rather than only be noted: ``consent_given`` could be set back to false and
    nothing anywhere read it again, so an account could keep ingesting documents and running
    the reasoning engine over a chart whose consent had been withdrawn -- while
    ``regulatory_service`` published "Explicit consent captured before clinical data is
    stored" in the same compliance summary a DPDP or CDSCO reviewer reads.

    Deliberately scoped to *new* processing. Reading the existing record, its audit trail, and
    the deterministic drug-safety checks over data already lawfully held all stay available:
    withdrawal is not erasure (that is ``soft_delete``), medical records carry retention
    obligations of their own, and a consent flag that silently switched off the allergy hard
    block would be a safety defect wearing a privacy control's clothes.
    """

    status_code = 403
    code = "consent_withdrawn"


class UnsupportedQueryParameterError(AetherError):
    """A query parameter that is no longer accepted was supplied."""

    status_code = 400
    code = "unsupported_query_parameter"


class ValidationError(AetherError):
    """Some of the details submitted could not be accepted. Check the highlighted fields."""

    status_code = 422
    code = "validation_error"


class UnsupportedFileTypeError(ValidationError):
    """This file type cannot be read. Upload a PDF, or a JPEG/PNG/WebP/HEIC image —
    a phone photo or flatbed scan of the prescription or report works."""

    code = "unsupported_file_type"


class FileTooLargeError(ValidationError):
    """This file is too large to upload. Upload the pages as separate files, or re-scan at a
    lower resolution.

    Raise sites pass a message naming the actual limit (see
    ``document_service.file_too_large_message``); this default only covers a raise without one.
    """

    code = "file_too_large"


class CorrectionNotApplicableError(ValidationError):
    """A correction could not be applied to the extracted item it names, so nothing was
    approved. Reload the extraction and make the change again.

    Raised rather than skipped because skipping is silent and wrong in the same breath: the
    approval answers 200, the clinician reads that as "my amendment is in the chart", and what
    merged was the *original* extracted value. A correction is a clinician overruling the
    extractor — most often on a dose or a lab value, which are exactly what the deterministic
    safety checks then read — so losing one quietly is worse than refusing the approval.

    Reachable when the client posts against an extraction it no longer has: a field name the
    entity does not carry, or an entity index past the end of the list.
    """

    code = "correction_not_applicable"


class HardBlockError(AetherError):
    """A safety hard block prevents this action. It can only be passed by an explicit
    clinician override with documented reasoning — it is never bypassed silently."""

    status_code = 409
    code = "hard_block"
