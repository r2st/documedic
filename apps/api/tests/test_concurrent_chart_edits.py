"""Two clinicians amending one chart at the same moment.

The arrangement this product is built for is one practice login used from two rooms, so two
people holding the same chart open is the ordinary way to work rather than a race to be
engineered away. ``PATCH /patients/{id}`` takes no lock and carries no version token, which
means its concurrency behaviour is whatever falls out of "partial update, own transaction" — and
that was true, correct, and written down nowhere. An emergent property is not a contract: the
next change to this method could turn a field-level merge into a whole-record overwrite without
a single test noticing.

So the behaviour is pinned here, in the three parts that matter:

* disjoint fields merge, because that is the case that actually happens — one clinician corrects
  a phone number while the other records consent;
* the same field is last-write-wins, deliberately, because a 409 on a demographic correction
  sends a clinician back to a form to resolve a conflict that is nearly always two people fixing
  the same typo;
* both writes are audited, which is what makes losing an edit recoverable rather than silent,
  and is the reason last-write-wins is acceptable for demographics and would not be acceptable
  for clinical content.

Clinical content does not come through here at all. It is merged from an approved extraction,
and that path *does* serialise — see ``test_concurrent_chart_merge``, which is about the failure
that costs a patient their anticoagulant.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.patient import Patient
from app.models.user import Account
from app.schemas.patient import PatientUpdate
from app.services.patient_service import PatientService


async def _chart(sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessionmaker() as db:
        account = Account(email=f"two-rooms-{uuid.uuid4().hex}@example.com", password_hash="x")
        db.add(account)
        await db.flush()
        patient = Patient(
            account_id=account.id,
            full_name="Asha Reddy",
            phone="+91 90000 00000",
            consent_given=False,
            consent_given_at=None,
        )
        db.add(patient)
        await db.flush()
        await db.commit()
        return account.id, patient.id


async def _read(sessionmaker, patient_id: uuid.UUID, account_id: uuid.UUID) -> Patient:
    async with sessionmaker() as db:
        return await PatientService(db).get_for_display(account_id, patient_id)


async def test_two_clinicians_editing_different_fields_both_keep_their_edit(sessionmaker):
    """The case that actually occurs. Neither edit may be overwritten by the other's stale read.

    Both sessions read the chart before either writes — the interleaving a lock would prevent —
    and then write disjoint fields.
    """
    account_id, patient_id = await _chart(sessionmaker)

    async with sessionmaker() as first, sessionmaker() as second:
        first_service, second_service = PatientService(first), PatientService(second)
        # Both read the pre-edit chart.
        await first_service.get(account_id, patient_id)
        await second_service.get(account_id, patient_id)

        await first_service.update(account_id, patient_id, PatientUpdate(phone="+91 98765 43210"))
        await second_service.update(account_id, patient_id, PatientUpdate(consent_given=True))

    chart = await _read(sessionmaker, patient_id, account_id)
    assert chart.phone == "+91 98765 43210", "the second clinician's write reverted the first's"
    assert chart.consent_given is True
    assert chart.consent_given_at is not None


async def test_a_field_neither_clinician_sent_is_left_alone(sessionmaker):
    """``exclude_unset`` is what makes the merge field-level rather than record-level.

    A PATCH that omitted a field must not write the value it happened to read for it — which is
    exactly how a whole-record overwrite loses the other clinician's edit.
    """
    account_id, patient_id = await _chart(sessionmaker)

    async with sessionmaker() as db:
        await PatientService(db).update(
            account_id, patient_id, PatientUpdate(phone="+91 11111 11111")
        )

    chart = await _read(sessionmaker, patient_id, account_id)
    assert chart.full_name == "Asha Reddy"
    assert chart.consent_given is False


async def test_the_same_field_edited_twice_is_last_write_wins(sessionmaker):
    """Stated, not discovered. There is no version token and no 409; the later write stands."""
    account_id, patient_id = await _chart(sessionmaker)

    async with sessionmaker() as first, sessionmaker() as second:
        await PatientService(first).get(account_id, patient_id)
        await PatientService(second).get(account_id, patient_id)
        await PatientService(first).update(
            account_id, patient_id, PatientUpdate(phone="+91 11111 11111")
        )
        await PatientService(second).update(
            account_id, patient_id, PatientUpdate(phone="+91 22222 22222")
        )

    chart = await _read(sessionmaker, patient_id, account_id)
    assert chart.phone == "+91 22222 22222"


async def test_the_overwritten_edit_is_still_in_the_audit_trail(sessionmaker):
    """What makes last-write-wins acceptable here.

    The losing edit is not lost — it is a `patient_updated` entry naming the field it changed, so
    a clinician who finds their correction gone can see that it landed and what replaced it. An
    overwrite with no record of the overwritten write would be a different proposition.
    """
    account_id, patient_id = await _chart(sessionmaker)

    async with sessionmaker() as first, sessionmaker() as second:
        await PatientService(first).update(
            account_id, patient_id, PatientUpdate(phone="+91 11111 11111")
        )
        await PatientService(second).update(
            account_id, patient_id, PatientUpdate(phone="+91 22222 22222")
        )

    async with sessionmaker() as db:
        entries = (
            (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.patient_id == patient_id, AuditLog.action == "patient_updated")
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )

    assert len(entries) == 2, "one of the two concurrent edits left no trace"
    assert all(e.payload["changed_fields"] == ["phone"] for e in entries)
    # The values themselves are deliberately absent: audit_logs is unencrypted and a phone number
    # is a direct identifier. What is recoverable is that the field was changed, and when.
    assert all("+91" not in str(e.payload) for e in entries)


async def test_consent_withdrawal_is_not_reinstated_by_a_concurrent_edit(sessionmaker):
    """The one field where losing a write would be more than an inconvenience.

    Consent is the lawful basis for holding everything else under the DPDP Act. A withdrawal
    racing an unrelated demographic correction must not be undone by it — which it is not,
    because the correction does not carry a consent field at all.
    """
    account_id, patient_id = await _chart(sessionmaker)
    async with sessionmaker() as db:
        await PatientService(db).update(account_id, patient_id, PatientUpdate(consent_given=True))

    async with sessionmaker() as first, sessionmaker() as second:
        await PatientService(first).get(account_id, patient_id)
        await PatientService(second).get(account_id, patient_id)
        await PatientService(first).update(
            account_id, patient_id, PatientUpdate(consent_given=False)
        )
        await PatientService(second).update(
            account_id, patient_id, PatientUpdate(phone="+91 98765 43210")
        )

    chart = await _read(sessionmaker, patient_id, account_id)
    assert chart.consent_given is False, "a phone-number edit reinstated withdrawn consent"
    assert chart.phone == "+91 98765 43210"


async def test_a_second_grant_does_not_move_the_timestamp_of_the_first(sessionmaker):
    """Consent was given once. Two clinicians recording it must not rewrite when it happened."""
    account_id, patient_id = await _chart(sessionmaker)

    async with sessionmaker() as db:
        await PatientService(db).update(account_id, patient_id, PatientUpdate(consent_given=True))
    first_given_at = (await _read(sessionmaker, patient_id, account_id)).consent_given_at
    assert first_given_at is not None

    async with sessionmaker() as db:
        await PatientService(db).update(account_id, patient_id, PatientUpdate(consent_given=True))

    assert (await _read(sessionmaker, patient_id, account_id)).consent_given_at == first_given_at
