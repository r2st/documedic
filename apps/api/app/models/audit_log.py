"""AuditLog model — append-only, SHA-256 hash-chained log of all clinical actions (P1-09)."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

# Every action type written to the immutable trail, and nothing else. Authentication events
# are part of the access trail (who reached patient data, and every failed/blocked attempt
# to), and so are the ``*_viewed`` reads: under the DPDP Act the question asked of this trail
# months later is "who saw this patient's data", which a write-only trail cannot answer.
#
# Held to the code by ``tests/test_audit_phi_reads.py``, in both directions. A missing entry
# means an action nobody can look up; a spare one describes disclosure the system never makes.
AUDIT_ACTIONS = (
    "auth_signup",
    "auth_login_success",
    "auth_login_failed",
    "auth_login_locked_out",
    "auth_logout",
    "auth_logout_all",
    "auth_token_refreshed",
    "auth_refresh_token_reuse_detected",
    "auth_session_revoked",
    "auth_session_idle_expired",
    # A sign-in ended by the concurrent-session ceiling rather than by anyone asking. Separate
    # from the two revocation actions above precisely because nobody asked for it: an incident
    # review must be able to tell a sign-out the clinician performed from one performed on
    # their behalf. See AuthService._enforce_concurrent_session_limit.
    "auth_session_evicted",
    "auth_password_changed",
    "auth_password_change_failed",
    # The step-up prompt in front of chart deletion, bulk import and whole-record export. Both
    # outcomes are here, and the failure is the more interesting of the two: it can only be
    # produced by someone who already holds a live access token, so a run of them is a person
    # at a signed-in workstation guessing at the password of the clinician who left it open.
    # That is precisely the event this gate exists to notice, and nothing else in the trail
    # would show it — the sign-in was legitimate and the tokens are valid.
    "auth_reauthenticated",
    "auth_reauthentication_failed",
    # The reset flow. All four are recorded even though the endpoint tells the caller nothing:
    # the response is deliberately identical whether the address exists, is over its ceiling, or
    # got a token, so the trail is the *only* place a burst of reset attempts against one
    # clinician is visible. ``auth_password_reset_token_reused`` is the sharpest of them — the
    # legitimate holder has no reason to present a spent token twice.
    "auth_password_reset_requested",
    "auth_password_reset_throttled",
    "auth_password_reset_completed",
    "auth_password_reset_token_reused",
    # Retention housekeeping. Dead session rows are deleted; this is what makes their removal
    # accountable, since the rows themselves can no longer say they existed.
    "auth_sessions_purged",
    "auth_reset_tokens_purged",
    "critical_lab_value_detected",
    "critical_lab_value_not_evaluated",
    # A named clinician attesting that they have seen one panic value, and the read of the
    # panel-wide queue that shows them. The acknowledgement is the entry that matters: the
    # detection above says the system noticed, and until this exists nothing in the record says
    # a person did. On a post-incident review the interval between the two is the question.
    "critical_lab_value_acknowledged",
    "critical_lab_queue_viewed",
    "patient_created",
    "patient_updated",
    "patient_deleted",
    # One CSV upload, as a single event. The charts it creates each write their own
    # ``patient_created`` row — this is the row that says those N creations were one act by one
    # clinician from one file, which is not recoverable from N individual entries that look
    # exactly like N charts typed in by hand. It is also written when nothing was created: a
    # file that was entirely rejected, or a dry run, is a disclosure-free event that still
    # answers "who tried to load what into this account, and when".
    "patients_imported",
    # The encounter lifecycle. ``encounter_signed`` is the one that matters most: it is the
    # moment a clinician attested to a visit note, and from then on the row's content cannot
    # change. ``encounter_amended`` is written against the *superseded* encounter — the original
    # — so a reader walking one visit's trail sees that it stopped being current and which
    # encounter replaced it, without having to scan the whole chart for a row pointing back.
    # ``encounter_amendment_opened`` is separate from both because drafting a correction and
    # publishing it are different acts, days apart, and a draft that is never signed changes
    # nothing about the record.
    "encounter_created",
    "encounter_updated",
    "encounter_signed",
    "encounter_amendment_opened",
    "encounter_amended",
    # The SBAR handover lifecycle. ``handoff_sent`` and ``handoff_acknowledged`` are the pair
    # that matters: together they say who handed this patient to whom, at what time, against
    # which outstanding risks, and how long the patient spent handed-over-but-unreceived. That
    # interval is the number a post-incident review asks for and nothing else in the record
    # holds it. ``handoff_created``/``handoff_updated`` cover the draft, which has been handed
    # to nobody and changes nothing clinically — recorded because the prose is patient content
    # and a write to it is a write to the chart.
    "handoff_created",
    "handoff_updated",
    "handoff_sent",
    "handoff_acknowledged",
    # The discharge. ``discharge_summary_finalized`` is the entry that matters, and it is the
    # only place one question has an answer: finalising writes ordinary ``medication_events``
    # rows onto the chart, indistinguishable afterwards from prescribing typed in one line at a
    # time, so nothing else can say that those starts and stops were one decision by one named
    # clinician at one transition of care. The preview is recorded for the same reason
    # ``protocol_previewed`` is — it runs the whole deterministic engine over the chart and
    # returns what stands against it, which discloses this patient's allergies, interactions and
    # outstanding results. The draft pair covers prose that is patient content: a write to it is
    # a write to the chart even though it has been shown to nobody and changes nothing clinically.
    "discharge_summary_created",
    "discharge_summary_updated",
    "discharge_summary_previewed",
    "discharge_summary_finalized",
    # The clinic diary. ``appointment_booked`` and ``appointment_closed`` are the pair a later
    # reader needs: the second says whether the patient came, and a follow-up nobody attended is
    # the event that matters. ``appointment_rescheduled`` carries both ends of the move, because
    # the row afterwards holds only the new time and "this was moved from Tuesday" is what is
    # asked about a missed appointment. The two panel-wide reads are disclosures in the sense
    # this list means — the diary says which patients are booked and why, and the reminder queue
    # names them again — so both are recorded against the account rather than any one chart, in
    # the same way the critical-lab queue is.
    "appointment_booked",
    "appointment_rescheduled",
    "appointment_cancelled",
    "appointment_closed",
    "appointment_list_viewed",
    "appointment_diary_viewed",
    "appointment_reminders_viewed",
    # When a provider's week is declared to work. Not patient data — but the windows are what
    # decides whether a booking is flagged as out-of-hours, so "this slot raised no warning" is
    # only answerable later if the availability the arithmetic read at the time is on record.
    "provider_availability_recorded",
    "provider_availability_removed",
    # Applying a curated order set. ``protocol_applied`` is the entry that matters and it is
    # the only place one question has an answer: the medication events an application creates
    # are ordinary rows on the chart afterwards, indistinguishable from ones typed in one at a
    # time, so nothing else can say those five things were one decision. The preview is
    # recorded too — it runs the whole deterministic engine over the chart and returns what
    # stands against it, which is a disclosure of the chart's allergies and interactions.
    "protocol_previewed",
    "protocol_applied",
    "protocol_applications_viewed",
    "protocol_suggestions_viewed",
    # The patient portal. ``portal_record_viewed`` is attributed to the account that issued
    # the credential — the patient has no account id and an entry with no actor is one a review
    # cannot follow — and its payload carries ``by_patient`` so the trail does not read as the
    # practice having opened the chart at 3am.
    "portal_access_granted",
    "portal_access_revoked",
    "portal_access_listed",
    "portal_record_viewed",
    # Multi-provider encounters. The grant and the withdrawal are the entries an access review
    # runs on: who was given sight of a consultation belonging to a chart they do not own, by
    # whom, and when it stopped. ``shared_encounter_viewed`` is the read that answers "did they
    # actually look", which is a different question from "were they entitled to".
    "encounter_participant_added",
    "encounter_participant_removed",
    "encounter_participants_viewed",
    "shared_encounters_viewed",
    "shared_encounter_viewed",
    # Reading a chart's laboratory history as series. Clinical content — values, units and
    # reference intervals across years — so it is a PHI read like any other, not a "chart
    # rendering" that escapes the trail because nothing was written.
    # Searching a panel's notes. Counts only — never the query: a permanent, unencrypted
    # record of what a clinician was looking for, scoped to one chart, names a patient and a
    # suspicion in the same row.
    "clinical_notes_searched",
    "lab_trends_viewed",
    "lab_trend_viewed",
    "document_uploaded",
    "extraction_completed",
    "extraction_failed",
    "extraction_retried",
    "extraction_approved",
    "field_corrected",
    "graph_merged",
    "drug_safety_check",
    "drug_safety_hard_block_overridden",
    # One reconciliation of a whole medication list at a transition of care. The individual
    # drugs each write their own ``drug_safety_check`` — this is the row that says those N
    # checks were one act, at one transition, against one list, which is not recoverable from N
    # entries that look exactly like N drugs checked one at a time on the safety screen. The
    # transition itself (admission/discharge/transfer) is the part a later reviewer cannot
    # reconstruct from anything else in the record.
    "medications_reconciled",
    "reasoning_session_started",
    "reasoning_intake_answered",
    # The opening bookend of one *run* of the panel, distinct from the session being opened:
    # a session is opened once and can be run several times, by clinicians other than the one
    # who opened it. Written in the claim's own transaction, which is the only commit that
    # survives a run killed mid-flight — so this is what says an analysis happened at all when
    # neither of the two records below ever arrives. See ``ReasoningService._claim_for_run``.
    "reasoning_run_started",
    "reasoning_session_completed",
    "reasoning_session_failed",
    "clinical_suggestion_created",
    "clinician_decision_recorded",
    "hard_block_triggered",
    # A hard block that was documented on the chart *while the panel was deliberating*, and so
    # was invisible to it — caught by the re-check that runs before anything is written. Kept
    # apart from ``hard_block_triggered`` because it says something that entry cannot: this
    # run's reasoning was built on a chart that no longer existed by the time it was published.
    "reasoning_chart_changed_under_run",
    "validation_run_executed",
    "safety_report_filed",
    "regulatory_dossier_generated",
    # PHI disclosures. Every endpoint that returns a patient's clinical data to a clinician
    # records one of these, so the trail answers "who saw this" and not only "who changed it".
    "patient_viewed",
    "patient_record_viewed",
    # The encounter reads. Both return presenting complaints and consultation notes, which is
    # clinical content, so both are disclosures in the sense this list means.
    "encounter_viewed",
    "encounter_list_viewed",
    # Both handover reads are disclosures. The list returns SBAR prose about the patient; the
    # checklist returns how many allergies, live medications and unacknowledged panic values a
    # named patient's chart holds, which is clinical content even though it is only counts.
    "handoff_list_viewed",
    "handoff_checklist_viewed",
    # The widest disclosure this API performs: the whole chart, in one file, leaving the system.
    "patient_record_exported",
    # A handover summary of a chart. A disclosure in the sense this list means — it returns the
    # active problems, current medications, recent labs and recent visits — and the only one
    # that also puts that content through a third-party provider, which is a fact about the
    # read a DPDP reviewer needs on the trail rather than inferable from the route name.
    "clinical_summary_generated",
    # A patient's whole prescribing history, assembled per drug: every start, stop and dose
    # change, with the visit each was recorded at. Narrower than the record export and wider
    # than the paged chart read.
    "prescription_timeline_viewed",
    "document_downloaded",
    "document_list_viewed",
    "extraction_viewed",
    "drug_safety_flags_viewed",
    "clinical_suggestions_viewed",
    "patient_pathways_viewed",
    # Not a disclosure itself — the issue of a *credential* that authorises one. The stream
    # token is minted against one patient's reasoning session and then travels in a query
    # string, where proxy access logs and browser history record it verbatim; that is why it
    # is short-lived and session-bound. Every read of that session's output is recorded, so the
    # act of handing out the key to it should be too: without this entry, a token minted and
    # used from somewhere else leaves a trail that begins mid-stream with no record of who
    # asked for it.
    "reasoning_stream_token_minted",
)


class AuditLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One immutable audit entry.

    Tamper-evidence: ``record_hash = sha256(prev_hash || canonical_payload)``. Each entry
    chains to the previous entry's hash, so any retroactive edit breaks the chain. Rows are
    append-only — the table has no ``updated_at``/``is_deleted`` and the service never updates.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        # The per-patient audit view reads WHERE patient_id = ? ORDER BY sequence DESC with a
        # LIMIT. This table is append-only and never pruned, so it becomes the largest in the
        # system; carrying the sort column in the index keeps that read from degrading.
        Index("ix_audit_logs_patient_sequence", "patient_id", text("sequence DESC")),
    )

    # Monotonic per-table sequence used to order the hash chain deterministically.
    # Assigned by the audit service (max+1) under a row lock, not DB autoincrement,
    # because this is not the primary key.
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=True
    )
    # No single-column index: ix_audit_logs_patient_sequence leads with patient_id and so
    # answers every lookup a plain (patient_id) index could. See migration 0009.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The record_hash of this patient's *previous* entry — a second chain, running through one
    # chart's rows only, alongside the global one in ``prev_hash``.
    #
    # It exists because the per-patient verification endpoint could not deliver what it says.
    # ``prev_hash`` links a row to whatever was appended before it anywhere in the system, so a
    # walk restricted to one patient cannot check linkage at all: it can only recompute each
    # surviving row's own hash, which a *deleted* row passes trivially. Removing the entry that
    # records who exported a chart is the tamper that matters — nobody edits an audit row, they
    # delete it — and ``GET /patients/{id}/audit/verify`` answered ``chain_valid: true`` for it
    # while its own documentation promised the opposite.
    #
    # NULL on every row written before migration 0025, and excluded from the hash when NULL so
    # those rows still verify byte-identically. A patient's first post-migration entry chains to
    # its legacy predecessor, so the two eras join up rather than restarting.
    patient_prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Deliberately unindexed. Verification recomputes each hash from the row it already holds
    # (app.core.audit_hash) and never searches by hash, so an index here only cost writes on
    # the busiest table in the system. See migration 0009.
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
