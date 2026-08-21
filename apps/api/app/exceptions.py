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


class EncounterNotFoundError(NotFoundError):
    """That visit is not on this chart. Open the encounter list to see what is."""

    code = "encounter_not_found"


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


class InvalidResetTokenError(AuthError):
    """This password-reset link is no longer valid. Request a new one from the sign-in screen.

    Deliberately one message for all four ways a reset can fail — unknown token, expired,
    already spent, superseded by a later request. Distinguishing them tells a holder of a
    token they should not have which of those it is, and "already used" in particular confirms
    that the address is a real account someone is actively resetting. The ``detail`` carries
    the distinction to the log, where it belongs.
    """

    code = "invalid_reset_token"


class ForbiddenError(AetherError):
    """This account does not have access to that record."""

    status_code = 403
    code = "forbidden"


class ReauthenticationRequiredError(AetherError):
    """Confirm your password to continue. This action needs it because of what it does to the
    record, and it has been a while since you signed in.

    A 403 rather than a 401, and the distinction is the whole point of the class. A 401 means
    "your credential is not good"; every client in this codebase answers one by rotating its
    refresh token and retrying, and the frontend answers a failed rotation by signing the
    clinician out. Neither is right here: the access token is perfectly valid, the sign-in is
    live, and nothing about it should be discarded. What is being asked for is a fresh proof
    that the person at the keyboard is the person the token belongs to, which is one password
    prompt and not a re-login. Sending a 401 would have logged clinicians out of a session that
    was never in question.

    ``retry_after`` is deliberately absent for the same reason: this does not clear by waiting.
    """

    status_code = 403
    code = "reauthentication_required"


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


class ConcurrentAnswerError(ConflictError):
    """Another answer to one of these questions was recorded at the same moment. Reload the
    intake to see what is on it, and re-enter anything that is missing.

    Raised when the insert is refused by ``uq_intake_answers_question`` twice running — the
    database refusing to record two answers of record for one clarifying question.
    ``ReasoningService.submit_answers`` retries once, which is enough for every ordinary
    collision (a double-clicked Submit, a retried request, the same case open in two rooms),
    so reaching this means a third submission arrived inside the second attempt's window.

    409 rather than letting the duplicate through, because the two rows cannot be told apart
    afterwards: they share a ``created_at`` and the primary key is a random UUID, so "the latest
    answer" is not a question the schema can answer. The engine would then read whichever one
    the query happened to return first — and on a ``red_flag`` question that is what decides
    whether the can't-miss sentinel screens a time-critical diagnosis in or out. A conflict the
    clinician can see and resolve is the correct answer; a coin toss over a red flag is not.
    """

    code = "concurrent_answer"


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


class ReasoningRunSupersededError(ConflictError):
    """This run took longer than a reasoning session is held for, and another run has since
    started on the case. Nothing from it has been saved. Watch the run that is going, or start
    another once it finishes.

    The other end of the single-run claim. ``_claim_for_run`` makes a run exclusive *at the
    moment it starts*; the claim then holds only for ``settings.reasoning_run_lease_minutes``,
    after which any other request may take the session over — that lease exists because the
    ordinary way a run ends is a Reasoning Theatre tab closing, which cancels the worker with a
    ``CancelledError`` that no failure handler sees and no claim release survives.

    So the lease has to expire on a dead run, and a live run can outlast it. The comment sizing
    it said a full panel's ceiling was ``llm_request_timeout_seconds`` per call; the real ceiling
    is that timeout times the retries times the providers in the fallback chain, which is an
    order of magnitude more, and it is reached exactly when a provider stops answering without
    closing the connection.

    What such a run must not do is publish: a second full set of ClinicalSuggestion rows under
    one session_id, immutable by trigger and impossible to clean up, over a header describing the
    successor. Today an incidental row lock held across the panel prevents the takeover from
    happening in the first place — see ``ReasoningService._finish_claimed_run``, which explains
    why that is coincidence rather than design, and re-asserts the claim as an explicit
    compare-and-swap so a run that no longer holds the session discards its output instead.
    """

    code = "reasoning_run_superseded"


class ExtractionInProgressError(ConflictError):
    """This document is still being read. Wait for that to finish before asking for it again.

    Raised by the extraction retry path when the document is in ``processing`` and its
    ``extraction_started_at`` is recent enough that a request could still be working on it. A
    second extraction of the same document is not a second opinion: both would overwrite
    ``extraction_metadata``, and the clinician would be reviewing whichever one happened to
    finish last against a screen drawn from the other.

    A ``processing`` document older than ``settings.extraction_stall_minutes`` is *not* this
    error — nothing is working on it and it is reclaimed instead. See
    ``DocumentService.reclaim_stalled_extractions``.
    """

    code = "extraction_in_progress"


class ExtractionAlreadyApprovedError(ConflictError):
    """This document's extracted details are already in the patient's record, so it cannot be
    read again. Upload a corrected scan instead, or amend the record directly.

    Re-extracting an approved document would replace the reviewed, corrected extraction with a
    fresh one that has never been seen — while the entities merged from the *old* one are
    already charted. The review screen would then be offering a set of items to approve that no
    longer corresponds to what is in the chart, and approving it again would merge against
    ``GraphService``'s deduplication with different values than it deduplicated the first time.
    """

    code = "extraction_already_approved"


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


class RequestTooLargeError(AetherError):
    """That request was too large to process. If you are uploading a document, upload the
    pages as separate files or re-scan at a lower resolution.

    413 rather than 422 because this is refused on the envelope, before anything has looked at
    what the body says — the point is that nothing ever does. Distinct from
    ``FileTooLargeError``, which is the upload route's own 20 MB rule applied to a file it has
    already accepted and begun reading; this is the ceiling on any request body at all, and it
    is the only one of the two that protects the unauthenticated routes.
    """

    status_code = 413
    code = "request_too_large"


class RequestTooDeeplyNestedError(AetherError):
    """That request could not be read because its structure was nested too deeply, so nothing
    was changed. Reload the page and try again.

    400 rather than 413: the body was within its size ceiling, so this is not a complaint
    about how much was sent. It is refused unparsed, while the body is still arriving, because
    JSON nesting is parsed recursively and the depth is knowable from the raw bytes.

    Distinct from the flat 400 FastAPI produces for a body it failed to parse, which is what a
    deeply nested body used to become: that answer is indistinguishable from a syntax error,
    carries no ``code`` for a client to match on, and only happened at all because CPython's
    recursion limit ran out first. Nothing this API accepts nests past three levels, so a body
    that nests past ``settings.max_json_depth`` is not a client this endpoint has.
    """

    status_code = 400
    code = "request_too_nested"


class NotReadyError(AetherError):
    """This server is not currently able to serve requests. Try again in a moment.

    The readiness probe's answer when a backing service it cannot work without does not
    respond in time. 503 rather than the generic 500 the unhandled path would otherwise
    produce, because the two mean opposite things to whatever is reading the probe: a 500 is
    "this instance is broken", a 503 is "this instance is not ready *yet*", and a load balancer
    holds the instance out of rotation on the second without treating it as a failed deploy.

    The cause goes in ``detail``, never in the message. A readiness probe is answered before
    any authentication runs, so its body is public — which driver raised what against which
    host is exactly the deployment shape ``/health/dependencies`` was made authenticated to
    stop publishing.
    """

    status_code = 503
    code = "not_ready"


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


class NothingToApproveError(ValidationError):
    """Nothing was read from this document, so there is nothing to approve into the record. Try
    reading it again, or enter the details by hand.

    Approving an extraction that produced no entities merges nothing and used to answer 200
    with ``{"merged": {}}`` — the same silent no-op that :class:`CorrectionNotApplicableError`
    and :class:`EntityNotMergeableError` exist to refuse, and with the worst reading of the
    three: the clinician is told the document is approved and moves on, when in fact the scan
    was never read at all. It also flipped ``extraction_status`` to ``completed``, erasing the
    one signal — on the document row and in every operator query over it — that the scan had
    never been read.

    Reachable two ways, and neither is recoverable by approving: extraction ``failed``
    outright, or it ran and found nothing (an unreadable scan, or a vision provider that was
    down while the deterministic parser found no text in the file either).
    """

    code = "nothing_to_approve"


class EntityNotMergeableError(ValidationError):
    """An extracted item was approved that nothing in the patient record can hold, so nothing
    was approved. Reject that item and approve the rest.

    Refused rather than skipped for the same reason as
    :class:`CorrectionNotApplicableError`, one level up: a silent skip answers 200 to a
    clinician who ticked "include in the record", and the item is in no chart, no count and no
    audit entry. Approval is the moment extraction becomes the patient's record, and it has to
    mean the same thing for every item it was given.

    Reachable only for documents extracted before the extractor was constrained to the types the
    graph charts — a model that volunteered ``"vital_sign"`` for a discharge summary's
    observations. Naming the index and the type lets the client reject exactly that item, so one
    unusable line does not cost the document its prescriptions.
    """

    code = "entity_not_mergeable"


class EncounterSignedError(ConflictError):
    """This visit has been signed, so its content can no longer be edited. Record an amendment
    instead — it keeps the signed note and adds your correction alongside it, with your reason.

    The signature is the point at which a clinician attested to what the note says, and a record
    that can be rewritten silently afterwards cannot support the question an amendment exists to
    answer: what did the chart say at the time the decision was made. So the edit is refused
    here, in ``EncounterService``, *and* by ``trg_encounters_signed_frozen`` on the encounter
    table (migration 0031) — the same belt-and-braces the immutable ``clinical_suggestions`` and
    ``drug_safety_overrides`` tables get, and for the same reason: a future writer that has not
    read this docstring must not be able to do it either.

    Not a 403. The caller is entitled to this chart; the record is in a state that does not
    admit the change, which is what 409 means.
    """

    code = "encounter_signed"


class EncounterTransitionError(ConflictError):
    """That is not a step this visit can take from where it is. Reload the encounter to see its
    current state.

    The lifecycle runs draft -> in_progress -> signed, and a signed encounter is superseded by
    an amendment rather than moved backwards. Every refused step is one of:

    * signing an encounter that is already signed — the second signature would either overwrite
      the first attestation's timestamp and signer, or record two people attesting to one note
      with no way to tell which the chart means;
    * moving a signed or amended encounter back to a draft state, which would take a clinical
      statement that has been attested to and quietly make it editable again;
    * amending an encounter nobody has signed. There is nothing to preserve, so the correction
      belongs in the draft itself — an amendment chain over unsigned drafts is a second history
      that says nothing the first does not.
    """

    code = "encounter_transition"


class EncounterAlreadyAmendedError(ConflictError):
    """Another amendment to this visit was signed first, so this one was not recorded. Reload
    the chart and amend the current version of the note.

    Raised when ``uq_encounters_one_signed_amendment`` refuses the second signed amendment of
    one encounter. Two clinicians may both open an amendment — that is ordinary, and drafts are
    deliberately unconstrained — but only one can become the successor, because "the current
    version of this visit" has to be a question the chart can answer. The losing amendment's
    text is still there as a draft; nothing the clinician typed is destroyed.
    """

    code = "encounter_already_amended"


class HardBlockError(AetherError):
    """A safety hard block prevents this action. It can only be passed by an explicit
    clinician override with documented reasoning — it is never bypassed silently."""

    status_code = 409
    code = "hard_block"


class HandoffSentError(ConflictError):
    """This handoff has been sent, so its content can no longer be edited.

    Sending is the point at which a clinician asserted "this is what I am handing over", and a
    handover note that can be rewritten afterwards is not a record of what was handed over —
    the same reasoning that freezes a signed encounter. A correction is a new handoff.

    Refused here *and* by ``trg_handoffs_sent_frozen`` on the table (migration 0037), the
    belt-and-braces every immutable table in this schema gets: a future writer that has not read
    this docstring must not be able to do it either.
    """

    code = "handoff_sent"


class HandoffChecklistStaleError(ConflictError):
    """The chart changed since this checklist was confirmed. Re-read it and send again.

    Raised when the checklist recomputed at send time does not match the one the clinician
    confirmed — a critical lab value arrived, a hard block was raised, a medication was charted
    between drafting the handover and sending it.

    Refused rather than silently re-snapshotted, because the checklist's entire value is that
    somebody looked. A handoff sent with a confirmation of a state that no longer holds is
    worse than one sent with no checklist at all: it carries a signed assertion that the
    outgoing clinician reviewed something they never saw. Same judgement as
    ``reasoning_chart_changed_under_run``, which refuses to publish reasoning built on a chart
    that has moved underneath it.
    """

    code = "handoff_checklist_stale"


class HandoffNotSentError(ConflictError):
    """Only a sent handoff can be acknowledged. A draft has not been handed to anyone yet.

    Acknowledging a draft would record a receipt for something the outgoing clinician is still
    writing, and would then freeze it mid-sentence.
    """

    code = "handoff_not_sent"


class AppointmentNotFoundError(NotFoundError):
    """That appointment is not on this chart. Open the patient's appointments to see what is."""

    code = "appointment_not_found"


class InvalidAppointmentSlotError(ValidationError):
    """That is not a usable appointment time. Check the start and end times and try again.

    Raised for the arithmetic failures :func:`app.core.scheduling.slot_problems` finds — an end
    before the start, a zero-length slot, a duration outside the configured bounds, a date
    further ahead than bookings are taken. The ``detail`` names the specific codes.

    A slot that merely starts in the *past* is not this error. Writing up a walk-in after the
    fact is ordinary clinic work, and refusing it would push people into backdating the clock
    on the visit instead — which loses the one thing the record is for.
    """

    code = "invalid_appointment_slot"


class AppointmentConflictError(ConflictError):
    """That provider already has a booking overlapping this time. Choose another slot, or
    cancel the existing booking first.

    Overlap is checked per *provider*, not per account: two clinicians in one practice seeing
    two patients at eleven o'clock is a normal Tuesday, and refusing it would make the diary
    unusable.

    409 rather than a warning, and this is the one scheduling refusal that is hard. A
    double-booked slot is not a data-quality problem the clinic sorts out later — it is two
    patients in a waiting room with one clinician, and the second of them found out by
    arriving. The clinician who genuinely means to overbook cancels or reschedules, which
    leaves a record of the decision; a soft warning leaves none.
    """

    code = "appointment_conflict"


class AppointmentNotOpenError(ConflictError):
    """This appointment has already been closed or cancelled, so it cannot be changed.

    A cancelled booking is not rescheduled — the slot has been released and may already be
    somebody else's — and a completed or missed one is a record of what happened, which is not
    editable for the same reason a signed encounter is not. Book a new appointment instead.
    """

    code = "appointment_not_open"


class ProtocolTemplateNotFoundError(NotFoundError):
    """No protocol template with that name. Open the template list to see what is available.

    The templates are curated content compiled into the application, not rows a deployment
    edits, so an unknown key is a stale link or a typo rather than a template somebody deleted.
    """

    code = "protocol_template_not_found"


class ProtocolTemplateUnusableError(ConflictError):
    """One of the medications in this template could not be matched to a known drug, so the
    template cannot be applied. The investigations and the follow-up are unaffected — apply
    them by deselecting the medication, and report the template so the drug can be added.

    A template naming a drug the vocabulary does not hold is a *configuration* fault, not a
    clinical one, and it is refused rather than worked around because the alternative is the
    failure this codebase has closed twice already: an unresolvable medication charted as
    though it had been checked. Nothing in the deterministic engine can evaluate a drug it
    cannot identify — no allergy cross-check, no interaction, no contraindication — so charting
    it from a template would put a medication on the record carrying the *appearance* of having
    passed the same checks as its neighbours.

    ``tests/test_protocol_templates.py`` refuses a template whose medications do not all resolve
    against the seeded vocabulary, which is where this should be caught; this is the runtime
    backstop for a vocabulary that has been edited since.
    """

    code = "protocol_template_unusable"


class LabMarkerNotFoundError(NotFoundError):
    """This chart holds no results for that marker. Open the lab trends list to see which
    markers have been reported for this patient.

    Distinct from an empty series on purpose. "No results for this analyte" and "a series with
    no comparable points" look identical in a 200 with an empty list, and they are different
    facts: the second means results exist and something — a unit nobody can convert, a missing
    sample date — kept them off the axis, which is a thing the clinician needs to see rather
    than a blank chart.
    """

    code = "lab_marker_not_found"


class ParticipantAccountNotFoundError(NotFoundError):
    """No account here uses that email address. Check the spelling with your colleague — they
    need an account on this system before a consultation can be shared with them.

    Named separately from :class:`PatientNotFoundError` so a client can tell "the colleague does
    not exist" from "the chart does not exist"; they call for completely different corrections
    and the API should not make the clinician guess which one happened.
    """

    code = "participant_account_not_found"


class ParticipantNotFoundError(NotFoundError):
    """That participation is not on this encounter. It may already have been withdrawn.

    Deliberately raised by an attempt to withdraw an already-withdrawn access rather than
    succeeding quietly: "I have just revoked this" and "somebody else revoked it in March" must
    not render as the same result to whoever is looking.
    """

    code = "participant_not_found"


class ParticipantAlreadyPresentError(ConflictError):
    """That colleague already participates in this consultation. Withdraw the existing
    participation first if their role has changed.

    A role change is a withdrawal and a fresh grant rather than an edit, so that the record
    keeps both spells and the dates each applied — an in-place update would leave the chart
    saying the consultant had been supervising all along.
    """

    code = "participant_already_present"


class ParticipantRoleNotPermittedError(AetherError):
    """That role cannot be used here. An author or a supervisor cannot be added to a note that
    has already been signed, and a colleague asked for an opinion cannot attest to the visit.

    A **403** rather than a 404 in both cases, and for the same reason: the caller has already
    been shown that the encounter exists, so there is nothing left to conceal, and being told
    "you may read this and may not sign it" is what lets them do the right thing next.
    """

    status_code = 403
    code = "participant_role_not_permitted"


class PortalAccessError(AuthError):
    """This link is not valid. It may have expired, or your clinic may have withdrawn it —
    contact them for a new one.

    One message for every way of failing: expired, revoked, issued against a chart that has
    since been withdrawn, and never a token at all. The caller is an unauthenticated member of
    the public holding a link, and telling them which of those it is would tell somebody who
    found the link on a shared phone what kind of thing they had found.
    """

    code = "portal_access_denied"


class PortalGrantNotFoundError(NotFoundError):
    """That portal link is not on this chart. It may already have been withdrawn.

    Raised by an attempt to withdraw an already-withdrawn grant rather than succeeding quietly:
    "I have just stopped this" and "this stopped in March" are answers a practice acts on
    differently.
    """

    code = "portal_grant_not_found"
