"""Whitespace could satisfy the documentation requirement on a hard-block override.

``DrugSafetyOverrideRequest.reasoning`` carried ``min_length=10``, which counts raw characters,
and ``SafetyService.override_hard_block`` stored ``reasoning.strip()``. Ten spaces cleared the
first and became ``""`` in the second.

That is the *only* sanctioned way past a hard block — a documented allergy or an absolute
contraindication, per Critical Safety Rule #3 — and the whole reason the path exists is that the
bypass is documented. The row written to ``drug_safety_overrides`` recorded that a clinician had
justified prescribing past the block while holding nothing they had said, and that append-only
row is what an adverse-event review or a CDSCO SaMD audit reads to reconstruct the decision. The
audit payload's ``reasoning_chars`` would have read ``0``.

The existing tests checked only that a *short* string ("ok") is rejected, which the raw
``min_length`` already did.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.safety import MIN_OVERRIDE_REASONING_CHARS, DrugSafetyOverrideRequest


def _request(reasoning: str) -> DrugSafetyOverrideRequest:
    return DrugSafetyOverrideRequest(drug_safety_check_id=uuid.uuid4(), reasoning=reasoning)


@pytest.mark.parametrize(
    "reasoning",
    [
        " " * MIN_OVERRIDE_REASONING_CHARS,
        " " * 40,
        "\t\t\t\t\t\t\t\t\t\t",
        "\n" * 12,
        "  \t \n  \r\n   ",
        "   ok     ",  # ten-plus raw characters around a two-character justification
    ],
    ids=["spaces", "many-spaces", "tabs", "newlines", "mixed", "padded-short"],
)
def test_whitespace_does_not_count_as_documented_reasoning(reasoning: str) -> None:
    with pytest.raises(ValidationError):
        _request(reasoning)


def test_real_reasoning_is_accepted_and_stored_stripped() -> None:
    """The validated value is the stored value, so the service's own strip is a no-op."""
    request = _request("  Rash was mild and non-IgE mediated; discussed with the patient.  ")

    assert request.reasoning == "Rash was mild and non-IgE mediated; discussed with the patient."


def test_the_floor_is_applied_to_the_stripped_text() -> None:
    """Exactly at the boundary, counted after stripping rather than before."""
    at_floor = "a" * MIN_OVERRIDE_REASONING_CHARS

    assert _request(f"   {at_floor}   ").reasoning == at_floor
    with pytest.raises(ValidationError):
        _request(f"   {'a' * (MIN_OVERRIDE_REASONING_CHARS - 1)}   ")
