"""Clinical dashboard aggregates: what is on this account's panel, in numbers.

Distinct from :mod:`app.services.metrics_service`, which measures *the engine* — session
throughput, autonomy-tier mix, verifier disagreement, citation faithfulness — for the Phase 4
monitored pilot. Nothing there answers "how many patients do I have, what am I seeing them for,
what am I prescribing, and what is the safety engine telling me most often". Those are questions
about the practice rather than about the software, and they had no endpoint at all.

Scoping is the whole risk here
------------------------------
Every count in this module crosses from a per-patient read to a per-*account* one, and an
aggregate is the easiest place in an API to leak across a tenancy boundary: a ``COUNT(*)`` with
a missing predicate returns a number rather than an error, and a number that is silently the
whole deployment's looks exactly like a number that is correctly this clinic's. So every query
below is anchored to ``patients.account_id`` — through a subquery on ``patient_id`` for the
tables that carry no account column of their own — and ``tests/test_dashboard_metrics.py``
asserts each one against a second account's data rather than trusting the shape of the SQL.

Soft deletes count as absences, not as rows. ``is_deleted`` is how a withdrawn chart leaves the
product (DPDP erasure is a withdrawal here, not a DELETE), and a dashboard that counted them
would report a panel the clinician cannot open.

Deterministic and offline: five aggregate queries, no LLM, no vector store.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql._typing import ColumnExpressionArgument

from app.models.drug_safety_check import DrugSafetyCheck
from app.models.encounter import Encounter
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient

# How many rows a "top N" answers with. A dashboard tile, not an export: a clinic's whole
# formulary is `GET ../record` per patient, and an unbounded GROUP BY over every medication row
# an account holds is the kind of query that is instant on a demo and not on a year of use.
DEFAULT_TOP_N = 10
MAX_TOP_N = 100

# What a medication row is called when neither name column holds anything. Grouped as one bucket
# rather than dropped, because "we prescribed 40 things we cannot name" is the finding.
UNNAMED_MEDICATION = "(unnamed)"

# Likewise for an encounter with no type recorded. The column is nullable and the extraction
# pipeline routinely cannot read one off a scanned note.
UNTYPED_ENCOUNTER = "(unspecified)"


class DashboardService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _patients_of(self, account_id: uuid.UUID) -> Select:
        """The account's live patient ids, as a subquery for the tables that carry no account.

        ``medication_events``, ``encounters`` and ``drug_safety_checks`` are all reached this
        way. ``drug_safety_checks`` does carry its own ``account_id`` — and is still filtered
        through here as well, because the two must agree and a row whose account column says one
        thing while its patient belongs to another is a row that should appear in neither
        account's numbers.
        """
        return select(Patient.id).where(
            Patient.account_id == account_id, Patient.is_deleted.is_(False)
        )

    async def patients(self, account_id: uuid.UUID) -> dict[str, Any]:
        """Panel size, broken down by the states a chart can actually be in.

        "Status" is not a column on ``patients`` — there is no lifecycle enum — so the breakdown
        is over the three properties that decide what a clinician can do with a chart: whether
        it has been withdrawn, whether consent to process it is recorded, and whether it carries
        the demographics the safety engine needs (a date of birth for every age-based check, a
        weight for a paediatric dose). Inventing a status enum to report on would have been a
        schema change in service of a dashboard; counting the states that already exist is the
        honest version.
        """
        live = Patient.is_deleted.is_(False)
        scoped = Patient.account_id == account_id

        async def count(*predicates: ColumnExpressionArgument[bool]) -> int:
            return int(
                await self.db.scalar(
                    select(func.count()).select_from(Patient).where(scoped, *predicates)
                )
                or 0
            )

        total = await count()
        withdrawn = await count(Patient.is_deleted.is_(True))
        return {
            # Every chart ever created on this account, withdrawn ones included, so the two
            # numbers below add up to something a reader can check.
            "total": total,
            "active": total - withdrawn,
            "withdrawn": withdrawn,
            "consent_recorded": await count(live, Patient.consent_given.is_(True)),
            "consent_missing": await count(live, Patient.consent_given.is_(False)),
            "date_of_birth_missing": await count(live, Patient.date_of_birth.is_(None)),
            "weight_missing": await count(live, Patient.weight_kg.is_(None)),
        }

    async def encounters(self, account_id: uuid.UUID) -> dict[str, Any]:
        """Visit volume by type and by lifecycle status.

        Two axes because they answer different questions: the type mix is what the clinic is
        seeing, and the status mix is how much of it has been attested to — a large ``draft``
        count is a backlog of visit notes nobody has signed, which is the operationally
        interesting number and is invisible in a total.
        """
        patients = self._patients_of(account_id)
        base = (Encounter.patient_id.in_(patients), Encounter.is_deleted.is_(False))

        by_type = await self.db.execute(
            select(Encounter.encounter_type, func.count())
            .where(*base)
            .group_by(Encounter.encounter_type)
        )
        by_status = await self.db.execute(
            select(Encounter.status, func.count()).where(*base).group_by(Encounter.status)
        )
        # Annotated rather than inferred: a Row unpacks as Any on the name column, and the
        # resulting ``dict[Any | str, int]`` makes the sort key below unresolvable.
        types: dict[str, int] = {(name or UNTYPED_ENCOUNTER): int(n) for name, n in by_type.all()}
        statuses = {str(status): int(n) for status, n in by_status.all()}
        return {
            "total": sum(types.values()),
            "by_type": dict(sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))),
            "by_status": dict(sorted(statuses.items(), key=lambda kv: (-kv[1], kv[0]))),
        }

    async def medications(
        self, account_id: uuid.UUID, *, limit: int = DEFAULT_TOP_N
    ) -> dict[str, Any]:
        """The most prescribed drugs on this panel, by number of charts rather than of rows.

        Counting distinct patients, not events: a titration that produced six ``change`` rows for
        one patient is one patient on that drug, and ranking by rows would put whichever drug is
        adjusted most often at the top of a list read as "what we prescribe most".

        Grouped on the generic name, falling back to the brand as written. Not resolved through
        the vocabulary here — that would be a per-row lookup over the whole account — so an
        unresolved brand appears under its own name. It is the same limitation the chart view
        has, and it is visible rather than hidden: the row is named as the chart wrote it.
        """
        limit = max(1, min(limit, MAX_TOP_N))
        name = func.coalesce(MedicationEvent.generic_name, MedicationEvent.brand_name_raw)
        result = await self.db.execute(
            select(
                name.label("drug"),
                func.count(func.distinct(MedicationEvent.patient_id)).label("patients"),
                func.count().label("events"),
            )
            .where(
                MedicationEvent.patient_id.in_(self._patients_of(account_id)),
                MedicationEvent.is_deleted.is_(False),
            )
            .group_by(name)
            .order_by(func.count(func.distinct(MedicationEvent.patient_id)).desc(), name)
            .limit(limit)
        )
        return {
            "limit": limit,
            "most_prescribed": [
                {
                    "drug": drug or UNNAMED_MEDICATION,
                    "patient_count": int(patients),
                    "event_count": int(events),
                }
                for drug, patients, events in result.all()
            ],
        }

    async def safety_flags(self, account_id: uuid.UUID) -> dict[str, Any]:
        """How often each deterministic check fires, and how much of it is a hard block.

        The distribution a clinical-safety lead reads to answer "which alert are my clinicians
        seeing forty times a day", which is the question that decides whether an alert is still
        being read. ``drug_safety_checks`` is append-only, so this is a cumulative count over the
        account's whole history rather than a snapshot of what is currently flagged — those are
        different numbers, and the standing per-chart view (`GET ../drug-safety/flags`) is the
        other one.
        """
        patients = self._patients_of(account_id)
        base = (
            DrugSafetyCheck.account_id == account_id,
            DrugSafetyCheck.patient_id.in_(patients),
        )
        by_type = await self.db.execute(
            select(
                DrugSafetyCheck.check_type,
                func.count().label("total"),
                # CASE rather than a cast of the boolean: PostgreSQL will not SUM a boolean and
                # SQLite will, so a cast here would pass the suite and fail in production.
                func.sum(case((DrugSafetyCheck.is_hard_block.is_(True), 1), else_=0)),
            )
            .where(*base)
            .group_by(DrugSafetyCheck.check_type)
            .order_by(func.count().desc(), DrugSafetyCheck.check_type)
        )
        by_severity = await self.db.execute(
            select(DrugSafetyCheck.severity, func.count())
            .where(*base)
            .group_by(DrugSafetyCheck.severity)
        )
        distribution = [
            {
                "check_type": str(check_type),
                "count": int(total),
                "hard_blocks": int(hard_blocks or 0),
            }
            for check_type, total, hard_blocks in by_type.all()
        ]
        severities = {str(severity): int(n) for severity, n in by_severity.all()}
        return {
            "total": sum(row["count"] for row in distribution),
            "hard_blocks": sum(row["hard_blocks"] for row in distribution),
            "by_check_type": distribution,
            "by_severity": dict(sorted(severities.items(), key=lambda kv: (-kv[1], kv[0]))),
        }

    async def overview(self, account_id: uuid.UUID, *, limit: int = DEFAULT_TOP_N) -> dict:
        """All four tiles in one round trip, for a dashboard that renders them together."""
        return {
            "patients": await self.patients(account_id),
            "encounters": await self.encounters(account_id),
            "medications": await self.medications(account_id, limit=limit),
            "safety_flags": await self.safety_flags(account_id),
        }
