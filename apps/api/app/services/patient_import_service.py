"""Loading a practice's patient list from a spreadsheet, one decided outcome per row.

The reading half — decoding, delimiter sniffing, the date-order inference and the per-cell
parsing — is :mod:`app.core.patient_import`, which is pure. This is the half that has to know
what is already on the account.

**Nothing here bypasses anything.** Every chart is built through the same
``PatientService.create_many`` the create form uses, so the consent gate, the encrypted columns
and the per-patient ``patient_created`` audit entry all apply exactly as they do to a chart typed
in by hand. The import adds one audit entry of its own — ``patients_imported`` — because N
identical creations are not recoverable from the trail as one act by one clinician from one file.

**The rule that decides what gets written: an import never creates a chart whose name is already
on the account.** Not "usually", and there is no override flag. The failure this is aimed at is
the one that matters clinically — a second chart for a patient who already has one, so that half
their allergies are on one record and half their prescriptions on the other, and every
deterministic safety check runs against half a patient. A duplicate that was skipped costs an
operator one line in a report; a duplicate that was created costs a clinician a hard block that
never fired. Skipped rows are reported individually, with which existing chart they matched, so
the operator can merge or rename and re-upload.

Matching is on the normalised name plus the date of birth, and the two cases where a date is
missing are reported apart from each other (``duplicate`` vs ``possible_duplicate``) because they
are different facts about the record and the operator's next action differs.

**Bounded and synchronous.** ``settings.patient_import_max_rows`` rows, one transaction, no
background job: an import that returns is an import that happened, and an operator watching a
progress bar for a job that half-succeeded is a worse product than a 500-row ceiling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.patient_import import (
    AmbiguousDateError,
    DateConvention,
    ImportFileError,
    ParsedRow,
    date_convention,
    normalise_name,
    parse_consent,
    parse_csv,
    parse_date,
    parse_decimal,
)
from app.exceptions import ValidationError
from app.models.patient import Patient
from app.schemas.patient import PatientCreate
from app.services.audit_service import AuditService
from app.services.patient_service import PatientService

RowStatus = Literal["created", "duplicate", "possible_duplicate", "invalid", "not_created"]


@dataclass
class RowOutcome:
    """What happened to one line of the file, and why.

    ``message`` is written for the person who will open the spreadsheet and fix it, so it names
    the column and quotes the value. It reaches only the clinician who uploaded the file — it is
    in the response body and never on the audit trail, which is unencrypted and holds counts.
    """

    row_number: int
    status: RowStatus
    full_name: str | None = None
    patient_id: uuid.UUID | None = None
    message: str | None = None
    # The chart this row matched, for the two duplicate statuses. Lets an operator open it.
    existing_patient_id: uuid.UUID | None = None


@dataclass
class ImportResult:
    dry_run: bool
    encoding: str
    delimiter: str
    date_convention: DateConvention | None
    unknown_columns: tuple[str, ...]
    total_rows: int
    created: int = 0
    skipped: int = 0
    invalid: int = 0
    rows: list[RowOutcome] = field(default_factory=list)


@dataclass(frozen=True)
class _Existing:
    """The identity of a chart already on the account, as the duplicate check compares it."""

    patient_id: uuid.UUID
    name_key: str
    date_of_birth: date | None


class PatientImportService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.patients = PatientService(db)

    async def import_csv(
        self,
        account_id: uuid.UUID,
        raw: bytes,
        *,
        dry_run: bool = False,
    ) -> ImportResult:
        """Read, validate, de-duplicate and (unless ``dry_run``) write. One transaction.

        ``dry_run`` runs every check and writes nothing, so an operator can see what a file will
        do before it does it. It costs the same as a real import minus the inserts, which is why
        it is metered on the same rate-limit bucket.
        """
        try:
            parsed = parse_csv(raw)
        except ImportFileError as exc:
            # A file-level problem, not a row-level one: reported as a 422 with the reader's own
            # message, which is written for the operator rather than for a log.
            raise ValidationError(str(exc), detail="patient import file rejected") from exc

        max_rows = settings.patient_import_max_rows
        if len(parsed.rows) > max_rows:
            raise ValidationError(
                f"This file has {len(parsed.rows)} patients. The importer takes up to {max_rows} "
                "at a time — split it and upload the parts.",
                detail=f"patient import of {len(parsed.rows)} rows over the {max_rows} ceiling",
            )

        try:
            convention = date_convention([row.get("date_of_birth") for row in parsed.rows])
        except ImportFileError as exc:
            raise ValidationError(
                str(exc), detail="patient import date order is contradictory"
            ) from exc

        result = ImportResult(
            dry_run=dry_run,
            encoding=parsed.encoding,
            delimiter=parsed.delimiter,
            date_convention=convention,
            unknown_columns=parsed.unknown_columns,
            total_rows=len(parsed.rows),
        )
        existing = await self._existing_charts(account_id)
        # Rows accepted so far in *this* file, checked against alongside the account's charts. A
        # list uploaded twice within one file is the same clinical failure as one uploaded twice
        # across two files, and only this catches it.
        accepted: list[_Existing] = []
        to_create: list[tuple[RowOutcome, PatientCreate]] = []

        for row in parsed.rows:
            outcome, payload = self._evaluate(row, convention)
            if payload is None:
                result.rows.append(outcome)
                result.invalid += 1
                continue
            match = self._match(payload, existing + accepted)
            if match is not None:
                status, existing_id = match
                outcome.status = status
                outcome.existing_patient_id = existing_id
                outcome.message = (
                    "A chart with this name and date of birth is already on this account; "
                    "nothing was created."
                    if status == "duplicate"
                    else (
                        "A chart with this name is already on this account, and one of the two "
                        "has no date of birth, so they cannot be told apart. Nothing was "
                        "created — add the date of birth to both and upload again if they are "
                        "different people."
                    )
                )
                result.rows.append(outcome)
                result.skipped += 1
                continue
            accepted.append(
                _Existing(
                    # A placeholder id: this row has not been written yet and may never be (a
                    # dry run), but the in-file duplicate check only ever compares the name and
                    # date, and giving it a real id would mean claiming a row exists.
                    patient_id=uuid.UUID(int=0),
                    name_key=normalise_name(payload.full_name),
                    date_of_birth=payload.date_of_birth,
                )
            )
            to_create.append((outcome, payload))

        if dry_run:
            for outcome, _payload in to_create:
                outcome.status = "not_created"
                outcome.message = "Valid, and would be created. Nothing was written — dry run."
                result.rows.append(outcome)
        else:
            created = await self.patients.create_many(
                account_id, [payload for _outcome, payload in to_create]
            )
            for (outcome, _payload), patient in zip(to_create, created, strict=True):
                outcome.status = "created"
                outcome.patient_id = patient.id
                result.rows.append(outcome)
            result.created = len(created)

        result.rows.sort(key=lambda r: r.row_number)
        await self.audit.record(
            action="patients_imported",
            account_id=account_id,
            entity_type="account",
            entity_id=account_id,
            # Counts and file shape only. Every name in this file is PII and `audit_logs` is
            # unencrypted, immutable and never pruned — the charts that were created each carry
            # their own `patient_created` row, and that is where a name would be if it belonged
            # anywhere, which it does not.
            payload={
                "dry_run": dry_run,
                "rows": result.total_rows,
                "created": result.created,
                "skipped": result.skipped,
                "invalid": result.invalid,
                "encoding": parsed.encoding,
                "date_convention": convention,
            },
        )
        await self.db.commit()
        return result

    def _evaluate(
        self, row: ParsedRow, convention: DateConvention | None
    ) -> tuple[RowOutcome, PatientCreate | None]:
        """One row to a validated ``PatientCreate``, or to a refusal carrying the reason."""
        name = row.get("full_name")
        outcome = RowOutcome(row_number=row.row_number, status="invalid", full_name=name or None)
        if not name:
            outcome.message = "full_name is empty."
            return outcome, None

        consent = parse_consent(row.get("consent_given"))
        if consent is None:
            outcome.message = (
                f"consent_given reads {row.get('consent_given')!r}, which this importer does not "
                "recognise. Use yes or no."
            )
            return outcome, None
        if not consent:
            # Not an error in the file — the operator said what they meant. It is still a refusal,
            # because consent is the lawful basis for holding the rest of the row (DPDP Act) and
            # `PatientService.create` refuses the same row typed in by hand.
            outcome.message = (
                "consent_given is no. A chart cannot be created without recorded consent, so "
                "this row was left out."
            )
            return outcome, None

        try:
            dob = parse_date(row.get("date_of_birth"), convention)
        except AmbiguousDateError as exc:
            outcome.message = f"date_of_birth: {exc}"
            return outcome, None
        except ValueError as exc:
            outcome.message = f"date_of_birth: {exc}"
            return outcome, None

        try:
            weight = parse_decimal(row.get("weight_kg"))
        except ValueError:
            outcome.message = f"weight_kg reads {row.get('weight_kg')!r}, which is not a number."
            return outcome, None

        sex = row.get("sex").casefold() or None
        try:
            payload = PatientCreate(
                full_name=name,
                date_of_birth=dob,
                # A cell that is not one of the four accepted words falls out of the
                # ``PatientCreate`` construction below as a ValidationError, which is reported
                # against the row like any other bad cell. Validating it here as well would be
                # two places to keep the vocabulary in.
                sex=sex,
                phone=row.get("phone") or None,
                address_text=row.get("address_text") or None,
                notes=row.get("notes") or None,
                consent_given=True,
                weight_kg=weight,
            )
        except PydanticValidationError as exc:
            # The same rules the create form enforces — name shape, plausible date of birth,
            # phone digits, weight bounds — reported per field so the operator can fix the cell
            # rather than the row. Pydantic's own wording is developer-facing, so only the field
            # and the message are lifted out of it.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            outcome.message = problems or "This row is not valid."
            return outcome, None
        return outcome, payload

    @staticmethod
    def _match(
        payload: PatientCreate, existing: list[_Existing]
    ) -> tuple[Literal["duplicate", "possible_duplicate"], uuid.UUID] | None:
        """The chart this row is already on the account as, if it is.

        Three cases, and the middle one is the reason this returns a status rather than a bool:

        * same name, same date of birth (or neither has one) — the same patient, as far as
          anything here can tell. ``duplicate``.
        * same name, one date of birth present and the other absent — not comparable. Reported
          as ``possible_duplicate`` and still not created, because "two charts for one patient"
          is a worse outcome than "one chart the operator has to add by hand".
        * same name, two different dates of birth — two people who share a name, which is
          ordinary. Not a match at all.
        """
        key = normalise_name(payload.full_name)
        fallback: tuple[Literal["possible_duplicate"], uuid.UUID] | None = None
        for candidate in existing:
            if candidate.name_key != key:
                continue
            if candidate.date_of_birth == payload.date_of_birth:
                return "duplicate", candidate.patient_id
            if candidate.date_of_birth is None or payload.date_of_birth is None:
                fallback = fallback or ("possible_duplicate", candidate.patient_id)
        return fallback

    async def _existing_charts(self, account_id: uuid.UUID) -> list[_Existing]:
        """Every live chart on the account, reduced to what the duplicate check compares.

        Whole-table for the account, and it has to be: ``full_name`` and ``date_of_birth`` are
        encrypted at rest with no blind index, so there is no ``WHERE full_name = ?`` to write —
        the comparison can only happen after SQLAlchemy has decrypted the values in Python. That
        is the cost of the encryption and it is the right trade; what makes it affordable is that
        this runs once per import (not once per row) and the route is metered per account per
        hour. A practice large enough for this to matter is one whose list this route should not
        be loading in a single request anyway, which the row ceiling already says.
        """
        result = await self.db.execute(
            select(Patient.id, Patient.full_name, Patient.date_of_birth).where(
                Patient.account_id == account_id,
                Patient.is_deleted.is_(False),
            )
        )
        return [
            _Existing(
                patient_id=row.id,
                name_key=normalise_name(row.full_name or ""),
                date_of_birth=row.date_of_birth,
            )
            for row in result.all()
        ]
