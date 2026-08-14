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
) -> str:
    """Deterministic JSON serialization of the hashed fields (sorted keys, no whitespace)."""
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
        )
        if (
            entry["prev_hash"] != expected_prev
            or compute_record_hash(entry["prev_hash"], canonical) != entry["record_hash"]
        ):
            valid = False
        expected_prev = entry["record_hash"]
    return valid, expected_prev


def verify_chain(entries: Sequence[dict[str, Any]]) -> bool:
    """Verify an ordered list of audit entries forms an unbroken hash chain from genesis.

    Returns True only if every link recomputes correctly and each ``prev_hash`` matches the
    prior row's ``record_hash``.
    """
    return verify_chain_from(entries, GENESIS_HASH)[0]
