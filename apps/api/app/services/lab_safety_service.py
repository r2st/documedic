"""Critical/panic lab-value surfacing service.

Wraps app.core.lab_safety with DB access: evaluates a patient's most recent result per marker
against curated critical-value ranges and appends an audit-trail entry for any finding. The
only I/O here is loading LabResult rows and appending to the audit log — no LLM call — so this
stays available even when AI reasoning is degraded/offline (Critical Safety Rule #8).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.lab_safety import (
    CriticalLabFlag,
    UnreadableLab,
    evaluate_critical_value,
    unreadable_lab,
)
from app.exceptions import NotFoundError, ValidationError
from app.models.critical_lab_acknowledgement import CriticalLabAcknowledgement
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.services.audit_service import AuditService

# Ceiling on one queue read. The row count is patients × distinct markers, so a practice with
# 400 charts and a dozen markers each is already past any page a clinician reads — the cap is
# there so the query cannot become unbounded as a panel grows, not because 2000 is a meaningful
# clinical number. A truncated queue reports itself; see ``CriticalQueue.truncated``.
MAX_QUEUE_ENTRIES = 2000


@dataclass(frozen=True)
class CriticalQueue:
    """The panel-wide outstanding-critical-value queue.

    ``acknowledged_count`` is carried alongside the entries rather than dropped, because "no
    outstanding critical values" and "no critical values at all" are different states and a
    clinician reading an empty queue should be able to tell which one they are looking at.

    ``truncated`` is the same statement about the other end. A safety queue that quietly stops
    at its limit is indistinguishable from one that had that many entries, and the entries it
    dropped are exactly as dangerous as the ones it kept.
    """

    entries: list[tuple[LabResult, CriticalLabFlag]]
    acknowledged_count: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class LabScreen:
    """What a critical-value screen found, and what it could not look at.

    The second half is not bookkeeping. ``evaluate_critical_value`` returns nothing both for a
    result inside the reference band and for one it could not place on a scale at all, so a
    screen reported as its flag list alone renders "this row was skipped" identically to "this
    row is fine" — see ``app.core.lab_safety.unreadable_lab``.
    """

    flags: list[tuple[LabResult, CriticalLabFlag]]
    unreadable: list[tuple[LabResult, UnreadableLab]]


class LabSafetyService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def _patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        """Fetch a patient the caller owns, or raise PatientNotFoundError.

        Delegates rather than repeating the predicate. This is an authorization check -- it is
        what stops one account reading another's records -- and it was previously copy-pasted
        into four services. Any future change to what "a patient this caller may read" means
        (an extra tenancy dimension, an account-status check) has to land in one place or it
        lands in three and misses the fourth.

        Imported inside the method: PatientService imports from this module's siblings, so a
        module-level import would close a cycle. Same pattern as DocumentService._get_patient.
        """
        from app.services.patient_service import PatientService

        return await PatientService(self.db).get(account_id, patient_id)

    async def check_patient_labs(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, audit: bool = True
    ) -> list[tuple[LabResult, CriticalLabFlag]]:
        """The critical/panic findings from :meth:`screen_labs`, without the skipped rows."""
        screen = await self.screen_labs(account_id=account_id, patient_id=patient_id, audit=audit)
        return screen.flags

    async def screen_labs(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, audit: bool = True
    ) -> LabScreen:
        """Evaluate the most recent value per marker for a patient against critical thresholds.

        Does not commit — callers own the transaction boundary (consistent with AuditService).
        """
        await self._patient(account_id, patient_id)
        # "Most recent per marker" is resolved in SQL by ROW_NUMBER(), not by reading the
        # patient's whole lab history into Python and dropping all but the first row of each
        # run. The old shape was unbounded on the wrong axis: what it transferred and
        # instantiated grew with every result ever ingested, while what it actually evaluates
        # is one row per distinct marker -- a set that stops growing once a patient's panels
        # settle. A chart with 3000 labs across 40 markers built 3000 ORM instances to use 40.
        #
        # The partition key is the *same* normalisation the old dict-dedup applied in Python.
        # It has to be: markers arrive from OCR of whatever format each lab prints, so one
        # patient accumulates "Potassium", "POTASSIUM" and " potassium". When the SQL sort key
        # was the raw ``marker_name`` and only the grouping was case- and whitespace-
        # insensitive, the spellings formed separate runs, the first run's newest row claimed
        # the shared key, and every later spelling was skipped as already seen -- so a panic
        # potassium recorded in a different case than an older normal one was dropped before it
        # ever reached evaluate_critical_value. Doing both in one place makes that class of
        # disagreement unrepresentable.
        #
        # Portability: ROW_NUMBER() OVER (PARTITION BY ...) and NULLS LAST in a window ORDER BY
        # are both plain SQL:2003 that PostgreSQL and SQLite (>= 3.30) each parse natively, so
        # this is one statement on both dialects rather than a dialect-specific DISTINCT ON.
        marker_key = func.lower(func.trim(LabResult.marker_name))
        ranked = (
            select(
                LabResult,
                marker_key.label("marker_key"),
                func.row_number()
                .over(
                    partition_by=marker_key,
                    order_by=(
                        LabResult.sample_date.desc().nullslast(),
                        # Two results for one marker from a single draw is ordinary -- a re-run,
                        # or the same panel entered from two documents. Without a tiebreaker
                        # which of them is "the latest" is whatever the planner returns first,
                        # and on a panic-value screen that is a coin flip between flagging a
                        # critical potassium and not. Most recently recorded wins, and ties on
                        # that resolve by primary key so the answer is at least the same answer
                        # every time.
                        LabResult.created_at.desc(),
                        LabResult.id,
                    ),
                )
                .label("marker_rank"),
            )
            .where(LabResult.patient_id == patient_id, LabResult.is_deleted.is_(False))
            .subquery()
        )
        latest = aliased(LabResult, ranked)
        # Ordering the survivors by the partition key keeps the flag list in a stable, marker-
        # alphabetical order. The old code got that incidentally from its ORDER BY; a window
        # function makes no promise about the order rows leave the outer query in, and the
        # audit payload built below is a record clinicians compare across requests.
        result = await self.db.execute(
            select(latest).where(ranked.c.marker_rank == 1).order_by(ranked.c.marker_key)
        )

        flagged: list[tuple[LabResult, CriticalLabFlag]] = []
        unreadable: list[tuple[LabResult, UnreadableLab]] = []
        for lab in result.scalars().all():
            if lab.value_numeric is None:
                continue
            value = float(lab.value_numeric)
            flag = evaluate_critical_value(lab.marker_name, value, lab.unit)
            if flag is not None:
                flagged.append((lab, flag))
                continue
            skipped = unreadable_lab(lab.marker_name, value, lab.unit)
            if skipped is not None:
                unreadable.append((lab, skipped))

        if audit and flagged:
            await self.audit.record(
                action="critical_lab_value_detected",
                account_id=account_id,
                patient_id=patient_id,
                entity_type="lab_result",
                # Structured values, not free text, so nothing here can carry an identifier —
                # but the row each finding came from was not recorded, which left the entry
                # unable to answer "which result was this" on a post-incident review. The
                # entry's own entity_id cannot carry it (a screen flags several results at
                # once), so the ids go in the payload alongside the values they describe.
                payload={
                    "flags": [
                        {
                            "lab_result_id": str(lab.id),
                            "marker_name": lab.marker_name,
                            "value": flag.value,
                            "unit": flag.unit,
                            "severity": flag.severity,
                        }
                        for lab, flag in flagged
                    ]
                },
            )
        if audit and unreadable:
            # A separate entry rather than a field on the one above, because it answers a
            # different question — not "what did the screen catch" but "what did it decline to
            # read" — and it is recorded even on a chart with no critical values at all, which
            # is precisely the chart where the omission would otherwise be invisible.
            await self.audit.record(
                action="critical_lab_value_not_evaluated",
                account_id=account_id,
                patient_id=patient_id,
                entity_type="lab_result",
                payload={
                    "unreadable": [
                        {
                            "lab_result_id": str(lab.id),
                            "marker_name": lab.marker_name,
                            "value": note.value,
                            "unit": note.unit,
                            "reason": note.reason,
                        }
                        for lab, note in unreadable
                    ]
                },
            )
        return LabScreen(flags=flagged, unreadable=unreadable)

    # --- The panel-wide queue -------------------------------------------------------------

    async def outstanding_critical_values(
        self, *, account_id: uuid.UUID, limit: int = MAX_QUEUE_ENTRIES
    ) -> CriticalQueue:
        """Every unacknowledged critical/panic value across this account's whole panel.

        The reader the detection never had. ``screen_labs`` answers "does *this* chart hold a
        panic value", which requires already having opened the chart; every other surfacing of
        a critical value has the same shape. A potassium of 6.8 extracted from a report uploaded
        overnight wrote its audit entry and then waited for someone to guess which patient to
        look at.

        **Evaluated live rather than from stored detections.** The alternative — persisting a
        row whenever ``screen_labs`` finds something and reading those back — is cheaper and
        wrong in a way that matters for a safety queue: it can only contain what was detected
        after the feature shipped, so it needs a backfill to be trustworthy on day one and is
        silently incomplete if the backfill misses anything. It would also drift, since a
        threshold correction in ``app.core.lab_safety`` changes what is critical and a stored
        detection would keep the old answer. This reads the labs and applies the current rules,
        which cannot be stale by construction.

        The cost is one window query over the account's latest-per-(patient, marker) rows, the
        same shape ``screen_labs`` uses for one patient with the partition widened. Bounded by
        ``limit``, and a truncated queue says so: a safety queue that quietly stops at n is
        indistinguishable from one with n entries in it.

        Never audits. Reading it discloses lab values across the panel, and the router records
        that once for the request — but this method is also the thing a caller polls, and an
        audit entry per poll would bury the trail it is supposed to be part of.
        """
        marker_key = func.lower(func.trim(LabResult.marker_name))
        ranked = (
            select(
                LabResult,
                marker_key.label("marker_key"),
                func.row_number()
                .over(
                    # Partitioned by patient *and* marker, where ``screen_labs`` partitions by
                    # marker alone. Everything else about the ordering is identical, and has to
                    # be: a queue that picked a different "latest result" than the chart screen
                    # does would show a clinician an entry that is not on the chart they open.
                    partition_by=(LabResult.patient_id, marker_key),
                    order_by=(
                        LabResult.sample_date.desc().nullslast(),
                        LabResult.created_at.desc(),
                        LabResult.id,
                    ),
                )
                .label("marker_rank"),
            )
            .join(Patient, Patient.id == LabResult.patient_id)
            .where(
                Patient.account_id == account_id,
                # A withdrawn chart drops off the queue. Consent withdrawal means this record is
                # not to be worked from, and a queue entry is an instruction to go and work from
                # it. Same judgement the rest of the read surface takes.
                Patient.is_deleted.is_(False),
                LabResult.is_deleted.is_(False),
                LabResult.value_numeric.is_not(None),
            )
            .subquery()
        )
        latest = aliased(LabResult, ranked)
        rows = (
            (
                await self.db.execute(
                    select(latest)
                    .where(ranked.c.marker_rank == 1)
                    # +1 so a full page is distinguishable from an exactly-full one, which is what
                    # lets ``truncated`` be honest rather than a guess.
                    .limit(limit + 1)
                    .order_by(ranked.c.marker_key)
                )
            )
            .scalars()
            .all()
        )

        truncated = len(rows) > limit
        flagged: list[tuple[LabResult, CriticalLabFlag]] = []
        for lab in rows[:limit]:
            if lab.value_numeric is None:
                continue
            flag = evaluate_critical_value(lab.marker_name, float(lab.value_numeric), lab.unit)
            if flag is not None:
                flagged.append((lab, flag))

        acknowledged = await self._acknowledged_lab_ids(
            account_id, [lab.id for lab, _flag in flagged]
        )
        return CriticalQueue(
            entries=[(lab, flag) for lab, flag in flagged if lab.id not in acknowledged],
            acknowledged_count=sum(1 for lab, _f in flagged if lab.id in acknowledged),
            truncated=truncated,
        )

    async def _acknowledged_lab_ids(
        self, account_id: uuid.UUID, lab_ids: list[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these results already carry an acknowledgement, in one query.

        Scoped to ``account_id`` as well as to the ids. The ids came from this account's own
        patients a moment ago, so the extra predicate changes no result — it is here because
        the alternative is a query whose correctness depends on where its arguments came from,
        and this one is read by a queue whose whole job is to not hide a dangerous value.
        """
        if not lab_ids:
            return set()
        rows = await self.db.execute(
            select(CriticalLabAcknowledgement.lab_result_id).where(
                CriticalLabAcknowledgement.account_id == account_id,
                CriticalLabAcknowledgement.lab_result_id.in_(lab_ids),
            )
        )
        return set(rows.scalars().all())

    async def acknowledge(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        lab_result_id: uuid.UUID,
        acknowledged_by: str,
        action_note: str | None,
    ) -> CriticalLabAcknowledgement:
        """Record that a named clinician has seen one critical value. Append-only.

        Refuses a result that is not currently critical (422). That is not pedantry: an
        acknowledgement is a clinical attestation, and one recorded against a normal potassium
        is a row in an append-only table asserting something that was never true. It also
        catches the client bug that would matter most — acknowledging by the wrong id, which
        would silently clear a *different* dangerous value off the queue.

        Acknowledging twice is permitted and inserts a second row. The queue keys on presence,
        so the second changes nothing about what a clinician sees; what it does is leave both
        attestations on the record, which is the correct outcome for a table nothing may edit.
        """
        await self._patient(account_id, patient_id)
        lab = await self.db.get(LabResult, lab_result_id)
        if (
            lab is None
            or lab.is_deleted
            or lab.patient_id != patient_id
            or lab.value_numeric is None
        ):
            raise NotFoundError(f"Lab result {lab_result_id} not found on this patient")

        flag = evaluate_critical_value(lab.marker_name, float(lab.value_numeric), lab.unit)
        if flag is None:
            raise ValidationError(
                "That result is not currently flagged as a critical or panic value, so there is "
                "nothing to acknowledge. Check the lab_result_id against the critical-value "
                "queue.",
                detail=f"lab_result_id={lab_result_id} is not critical",
            )

        row = CriticalLabAcknowledgement(
            account_id=account_id,
            patient_id=patient_id,
            lab_result_id=lab.id,
            marker_name=lab.marker_name,
            value=flag.value,
            unit=flag.unit,
            severity=flag.severity,
            acknowledged_by=acknowledged_by,
            action_note=action_note,
        )
        self.db.add(row)
        await self.db.flush()
        await self.audit.record(
            action="critical_lab_value_acknowledged",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="critical_lab_acknowledgement",
            entity_id=row.id,
            payload={
                # The value and its severity, which are the facts about the event; the marker
                # name, which is a lab test rather than anything about this person; the length
                # of the note rather than the note, which is a clinician's prose about a patient
                # and does not belong in an unencrypted append-only trail
                # (tests/test_audit_payload_free_text.py). ``acknowledged_by`` is the clinician's
                # own name, not the patient's, and is the whole point of the entry.
                "lab_result_id": str(lab.id),
                "marker_name": lab.marker_name,
                "value": flag.value,
                "unit": flag.unit,
                "severity": flag.severity,
                "acknowledged_by": acknowledged_by,
                "action_note_chars": len(action_note or ""),
            },
        )
        return row
