"""Who may do what on a shared encounter — the vocabulary and its capabilities, as pure data.

The problem this exists for
---------------------------
A chart in this product belongs to exactly one account (``patients.account_id``), and every
clinical route resolves through that ownership. That is the right default and it leaves no
middle setting: a consultant asked to advise on one consultation could be given the whole panel
or nothing at all. In practice that means the record is emailed, photographed or read out, which
is the failure mode a record system exists to remove.

The second half of the same gap is on the record rather than in access to it. ``encounters``
carries one ``signed_by_account_id``, so a note written by a registrar under supervision and a
note written by a consultant alone are the same row. Who else was present, who was asked, and
who countersigned are clinical facts about the visit, and none of them were anywhere.

Why the capabilities are here
-----------------------------
A role is a claim about what somebody may do to a clinical record, and the answer must not
depend on a database being reachable, on a settings file, or on which of several call sites
happened to check. It is a lookup in a frozen table, which is testable directly and cannot be
half-applied — the same reasoning that puts the drug-safety rules in ``app.core.safety``.

Two properties the table is built to have, both pinned by tests:

* **Read is universal and write is not.** Every role reads; exactly two may attest. A role added
  later that forgets to say whether it may sign inherits "may not", because the capability sets
  are enumerated per role rather than defaulted.
* **Nothing here grants access to the chart.** These capabilities are about one encounter. A
  participant is not a co-owner of the patient, and the routes are shaped so that participation
  cannot be widened into chart access by any combination of ids — see
  ``tests/test_multi_provider_encounter.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EncounterRole = Literal["author", "supervising", "consulting", "observing"]

# The roles, in the order a UI should offer them: most involved first.
ENCOUNTER_ROLES: tuple[EncounterRole, ...] = ("author", "supervising", "consulting", "observing")


@dataclass(frozen=True)
class RoleCapabilities:
    """What one role may do with the encounter it is a participant of.

    ``may_sign`` is the only one that changes the record, and it is deliberately the narrowest.
    Signing is an attestation — "I am accountable for what this note says" — so it belongs to
    the person who wrote it and to the person supervising them, and to nobody who was asked for
    an opinion or who was standing in the room.
    """

    may_read: bool
    may_sign: bool
    description: str


# Keyed by ``str`` rather than by ``EncounterRole`` on purpose. Every lookup below comes from
# the wire or from a database column, both of which are plain strings, and a table typed by the
# Literal forces each of those call sites to cast — which is a cast asserting exactly the fact
# the lookup exists to check. Typing the key as ``str`` keeps the check honest; the closed set
# is still stated by ``ENCOUNTER_ROLES`` and pinned against this dict by the tests.
_CAPABILITIES: dict[str, RoleCapabilities] = {
    "author": RoleCapabilities(
        may_read=True,
        may_sign=True,
        description=(
            "Wrote the note. Reads the visit and may attest to it. More than one account may "
            "hold this role: a note genuinely written by two people at a joint clinic is one "
            "note with two authors, and recording only the last one to type is a record of who "
            "was at the keyboard rather than of who took the history."
        ),
    ),
    "supervising": RoleCapabilities(
        may_read=True,
        may_sign=True,
        description=(
            "Supervises the author. Reads the visit and may countersign it — which is the whole "
            "point of the role, because a trainee's note attested only by the trainee reads "
            "afterwards as an unsupervised consultation."
        ),
    ),
    "consulting": RoleCapabilities(
        may_read=True,
        may_sign=False,
        description=(
            "Asked for an opinion on this visit. Reads it and does not attest to it: an opinion "
            "given on one question is not accountability for the whole note, and recording it "
            "as a signature would make it so."
        ),
    ),
    "observing": RoleCapabilities(
        may_read=True,
        may_sign=False,
        description=(
            "Present, and not clinically responsible — a student, or a colleague sitting in. "
            "Reads the visit and changes nothing about it."
        ),
    ),
}

# Roles that assert somebody took part in the consultation *as it happened*. Adding one of these
# after the note has been signed would rewrite who attended a visit that is already attested to,
# which is the same objection that freezes a signed encounter's content — so the service refuses
# it. ``consulting`` is deliberately not in this set: a second opinion sought a week later is an
# ordinary, honest thing to record against a signed note, and it changes no attestation.
ROLES_FROZEN_BY_SIGNATURE: frozenset[EncounterRole] = frozenset({"author", "supervising"})


def capabilities_for(role: str) -> RoleCapabilities:
    """The capabilities of ``role``.

    Raises ``KeyError`` for an unknown role rather than returning a permissive default. A
    typo'd role that reads as "may do everything" is the failure this whole module is meant to
    make impossible; failing loudly at the call site is the cheap alternative.
    """
    return _CAPABILITIES[role]


def may_sign(role: str) -> bool:
    """Whether ``role`` may attest to the encounter. False for anything unrecognised.

    Unlike :func:`capabilities_for`, this answers rather than raises for an unknown role,
    because it is the fail-closed form the authorization path wants: a role that is not in the
    table has not been granted the right to sign a clinical record.
    """
    entry = _CAPABILITIES.get(role)
    return entry is not None and entry.may_sign


def may_read(role: str) -> bool:
    """Whether ``role`` may read the encounter. False for anything unrecognised."""
    entry = _CAPABILITIES.get(role)
    return entry is not None and entry.may_read
