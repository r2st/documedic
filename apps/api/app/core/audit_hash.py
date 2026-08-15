"""Pure helpers for the immutable, hash-chained audit log (P1-09).

The genesis ``prev_hash`` and the record-hash construction live here so they can be
unit-tested and reused by an offline integrity verifier without touching the database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

# Hash that the very first audit row chains from.
GENESIS_HASH = "0" * 64


def canonical_payload(
    *,
    sequence: int,
    action: str,
    account_id: str | None,
    patient_id: str | None,
    entity_type: str | None,
    entity_id: str | None,
    payload: dict[str, Any],
    created_at: str,
    patient_prev_hash: str | None = None,
) -> str:
    """Deterministic JSON serialization of the hashed fields (sorted keys, no whitespace).

    ``patient_prev_hash`` is covered by the hash when it is set, and *absent from the body*
    when it is not. Two things depend on that asymmetry. It has to be hashed, or the
    per-patient link is a plain column an editor can retie to point anywhere. And it has to
    vanish rather than serialize as ``null``, because every row written before migration 0025
    was hashed by a version of this function that had no such key: including it as null would
    change their canonical form and fail the whole historical trail on the first verification
    after deploy — tamper evidence for a deploy, which is precisely the false positive that
    teaches an operator to stop reading it.
    """
    body = {
        "sequence": sequence,
        "action": action,
        "account_id": account_id,
        "patient_id": patient_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "payload": payload,
        "created_at": created_at,
    }
    if patient_prev_hash is not None:
        body["patient_prev_hash"] = patient_prev_hash
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def compute_record_hash(prev_hash: str, canonical: str) -> str:
    """record_hash = sha256(prev_hash || canonical_payload)."""
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("utf-8"))
    digest.update(canonical.encode("utf-8"))
    return digest.hexdigest()


def verify_chain_from(entries: Sequence[dict[str, Any]], expected_prev: str) -> tuple[bool, str]:
    """Verify a run of entries continues the chain from ``expected_prev``.

    Returns ``(valid, next_expected_prev)``, so a caller reading a long log in batches can carry
    the link across the batch boundary instead of holding the whole table in memory to check it.
    :func:`verify_chain` is this, starting from genesis.

    Each entry must provide the same fields passed to :func:`canonical_payload` plus
    ``prev_hash`` and ``record_hash``. A broken link does not stop the walk: the remaining
    entries are still checked on their own terms, so what comes back is "these links are wrong"
    rather than "everything after the first edit is wrong", which is the more useful thing to
    hand an operator holding tamper evidence.
    """
    valid = True
    for entry in entries:
        canonical = canonical_payload(
            sequence=entry["sequence"],
            action=entry["action"],
            account_id=entry.get("account_id"),
            patient_id=entry.get("patient_id"),
            entity_type=entry.get("entity_type"),
            entity_id=entry.get("entity_id"),
            payload=entry.get("payload", {}),
            created_at=entry["created_at"],
            patient_prev_hash=entry.get("patient_prev_hash"),
        )
        if (
            entry["prev_hash"] != expected_prev
            or compute_record_hash(entry["prev_hash"], canonical) != entry["record_hash"]
        ):
            valid = False
        expected_prev = entry["record_hash"]
    return valid, expected_prev


def verify_patient_chain_from(
    entries: Sequence[dict[str, Any]], expected_prev: str | None
) -> tuple[bool, str | None]:
    """Verify a run of ONE patient's entries, in sequence order, links to itself.

    The per-patient twin of :func:`verify_chain_from`, and the only check that notices a
    *deleted* entry in a chart's trail: recomputing a surviving row's own hash cannot, because
    a removed row leaves nothing behind to recompute. Here each entry's ``patient_prev_hash``
    must be the ``record_hash`` of the previous entry for the same patient, so removing one
    orphans the next.

    ``expected_prev`` is ``None`` at the start of a patient's trail, and is carried out again
    so a caller reading a long trail in batches can continue across the boundary.

    Rows written before migration 0025 carry no ``patient_prev_hash``. They are passed over
    rather than failed — they were written before there was a link to keep, and reporting the
    whole historical trail as tampered would drown the finding this function exists to make.
    Their ``record_hash`` still becomes the expectation for whatever follows, so the first
    linked row after the migration is checked against its legacy predecessor.

    A broken link does not stop the walk, for the same reason it does not in
    :func:`verify_chain_from`: an operator holding tamper evidence is better served by every
    bad link than by the first one.
    """
    valid = True
    for entry in entries:
        link = entry.get("patient_prev_hash")
        if link is not None and link != (expected_prev or GENESIS_HASH):
            valid = False
        expected_prev = entry["record_hash"]
    return valid, expected_prev


def verify_chain(entries: Sequence[dict[str, Any]]) -> bool:
    """Verify an ordered list of audit entries forms an unbroken hash chain from genesis.

    Returns True only if every link recomputes correctly and each ``prev_hash`` matches the
    prior row's ``record_hash``.
    """
    return verify_chain_from(entries, GENESIS_HASH)[0]
