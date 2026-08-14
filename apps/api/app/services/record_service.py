"""Longitudinal patient-record assembly (P1-06c).

Every section here is paged. It used to read each of the five collections in full, which was
fine for the charts this was written against and wrong for the ones it will meet: labs and
medication events accumulate per document ingested and are never pruned, so the response size
of ``GET /patients/{id}/record`` was a function of how long the patient had been a patient.
A twenty-year chart is not a page, and it was being assembled in memory, validated row by row
into Pydantic models, and serialised on every clinical screen that shows a timeline.

Two properties this module has to hold for paging to mean anything:

**A total order.** ``LIMIT``/``OFFSET`` over a sort with ties is not a partition of the set --
two rows that compare equal may swap between two requests, so a row can be served twice or
skipped entirely across a page boundary. Every ordering below therefore ends in the primary
key. It is clinically meaningless as a sort and that is the point: it is there to make the
comparison total, not to be read.

**A truthful count.** The section totals are counted, not inferred from the page length, so a
caller can tell a short page from a last page. See :class:`~app.schemas.record.RecordPagination`
for why that distinction is not left to the caller.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.allergy import Allergy
from app.models.base import Base
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.schemas.common import PaginationMeta
from app.schemas.record import (
    AllergyItem,
    ConditionItem,
    DerivedMarkerItem,
    LabItem,
    LongitudinalRecord,
    MedicationItem,
    RecordPagination,
)

# Sized for the read this serves: a clinician's chart view, which shows a timeline and a set of
# summary panels. 100 of each section covers the visible history for essentially every patient
# in the pilot while bounding the worst case, which is what this default exists to do.
DEFAULT_PAGE_LIMIT = 100

# The ceiling a client may ask for. Not a performance number -- it is the point past which a
# caller should be reading a section's own endpoint rather than pulling the whole chart across
# five collections at once.
MAX_PAGE_LIMIT = 500

# What the reasoning engine takes. Deliberately the ceiling rather than the default: the
# snapshot is the agents' entire view of the patient, and a bound tuned for what fits on a
# screen is not the right bound for what a differential is built from. It is still a bound --
# an unbounded read is what this module exists to remove -- and because the snapshot carries
# ``pagination``, a truncated one is visible to whatever reads it rather than silently short.
REASONING_SNAPSHOT_LIMIT = MAX_PAGE_LIMIT


class RecordService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _section[RowT: Base, ItemT: BaseModel](
        self,
        stmt: Select[tuple[RowT]],
        item_type: type[ItemT],
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[ItemT], PaginationMeta]:
        """Run one section's query as a counted page.

        The count is derived from ``stmt`` rather than rebuilt from the model, so the filter it
        counts and the filter it pages can never drift apart -- including the soft-delete
        predicate, which is the one that would be silently dropped and would inflate every
        total by the deleted rows. ``order_by(None)`` strips the sort first: ordering a count
        is wasted work, and it keeps these statements out of the ordered-read assertions in
        ``test_index_coverage``.
        """
        total = (
            await self.db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
        ) or 0
        rows = (await self.db.execute(stmt.limit(limit).offset(offset))).scalars().all()
        items = [item_type.model_validate(row) for row in rows]
        return items, PaginationMeta(
            total=total,
            limit=limit,
            offset=offset,
            # Not ``len(items) == limit``: on an offset past the end that is a false negative,
            # and on an exactly-full last page a false positive.
            has_more=offset + len(items) < total,
        )

    async def assemble(
        self,
        patient_id: uuid.UUID,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> LongitudinalRecord:
        """Assemble one page of each section of the patient's longitudinal record.

        ``limit`` and ``offset`` apply to every section independently -- see
        :class:`~app.schemas.record.RecordPagination`. A caller paging deep into one long
        section will therefore find the short ones empty, which is correct (there is no
        hundredth allergy) and is why the per-section totals are returned alongside.
        """
        meds, meds_page = await self._section(
            select(MedicationEvent)
            .where(
                MedicationEvent.patient_id == patient_id,
                MedicationEvent.is_deleted.is_(False),
            )
            .order_by(
                MedicationEvent.is_current.desc(),
                MedicationEvent.event_date.desc(),
                MedicationEvent.id,
            ),
            MedicationItem,
            limit=limit,
            offset=offset,
        )
        labs, labs_page = await self._section(
            select(LabResult)
            .where(LabResult.patient_id == patient_id, LabResult.is_deleted.is_(False))
            .order_by(
                LabResult.sample_date.desc().nullslast(),
                LabResult.marker_name,
                LabResult.id,
            ),
            LabItem,
            limit=limit,
            offset=offset,
        )
        conditions, conditions_page = await self._section(
            select(Condition)
            .where(Condition.patient_id == patient_id, Condition.is_deleted.is_(False))
            .order_by(Condition.status, Condition.condition_name, Condition.id),
            ConditionItem,
            limit=limit,
            offset=offset,
        )
        allergies, allergies_page = await self._section(
            select(Allergy)
            .where(Allergy.patient_id == patient_id, Allergy.is_deleted.is_(False))
            .order_by(Allergy.allergen_name, Allergy.id),
            AllergyItem,
            limit=limit,
            offset=offset,
        )
        markers, markers_page = await self._section(
            select(DerivedMarker)
            .where(DerivedMarker.patient_id == patient_id, DerivedMarker.is_deleted.is_(False))
            .order_by(DerivedMarker.computed_at.desc(), DerivedMarker.id),
            DerivedMarkerItem,
            limit=limit,
            offset=offset,
        )

        return LongitudinalRecord(
            patient_id=patient_id,
            medications=meds,
            lab_results=labs,
            conditions=conditions,
            allergies=allergies,
            derived_markers=markers,
            pagination=RecordPagination(
                medications=meds_page,
                lab_results=labs_page,
                conditions=conditions_page,
                allergies=allergies_page,
                derived_markers=markers_page,
            ),
        )
