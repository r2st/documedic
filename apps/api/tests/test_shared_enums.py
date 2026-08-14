"""The enums in ``app.schemas.common`` are a wire contract, not an internal convenience.

Their members are what the API puts on the wire and what ``packages/shared-types/src/enums.ts``
narrows the TypeScript client to. Two things can silently break that contract:

  * a member added, renamed or removed on one side of the language boundary only — the
    docstrings on both files say "must stay in sync", which is a request, not a check;
  * a change to how a member stringifies. These are ``StrEnum``, so ``f"{tier}"`` yields
    ``flag_for_review``. Under the older ``class Tier(str, Enum)`` spelling the very same
    f-string yields ``AutonomyTier.flag_for_review``, because a plain mixin enum keeps
    ``Enum.__str__``. Nothing in the type checker notices the difference, and every place
    that interpolates a tier into a log line, an SSE payload or a prompt would start
    emitting the Python repr instead of the value the client matches on.

So this locks both: member-for-member equality with the TypeScript unions, and the
stringification the rest of the codebase assumes.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import pytest

from app.schemas import common

ENUMS_TS = Path(__file__).resolve().parents[3] / "packages" / "shared-types" / "src" / "enums.ts"

# TS unions with no Python counterpart: SafetyCheckType is produced by the safety service as a
# plain string, so it is declared for the client without a schema enum behind it.
TS_ONLY = {"SafetyCheckType"}


def _python_enums() -> dict[str, type[StrEnum]]:
    return {
        name: obj
        for name, obj in vars(common).items()
        if isinstance(obj, type) and issubclass(obj, StrEnum) and obj is not StrEnum
    }


def _typescript_unions() -> dict[str, list[str]]:
    """Parse ``export type Name = 'a' | 'b';`` (single- or multi-line) out of enums.ts."""
    source = ENUMS_TS.read_text(encoding="utf-8")
    unions: dict[str, list[str]] = {}
    for match in re.finditer(r"export type (\w+)\s*=\s*([^;]+);", source):
        name, body = match.group(1), match.group(2)
        unions[name] = re.findall(r"'([^']+)'", body)
    return unions


def test_enums_ts_is_present_and_parses() -> None:
    """A rename or move of enums.ts must fail loudly here, not silently skip the sync check."""
    assert ENUMS_TS.is_file(), f"the canonical TypeScript enums are missing at {ENUMS_TS}"
    assert _typescript_unions(), "no `export type X = 'a' | 'b'` unions parsed out of enums.ts"


def test_every_python_enum_has_a_typescript_union() -> None:
    missing = sorted(set(_python_enums()) - set(_typescript_unions()))
    assert not missing, (
        f"these schema enums reach the client with no TypeScript union to narrow it: {missing}"
    )


def test_every_typescript_union_has_a_python_enum() -> None:
    extra = sorted(set(_typescript_unions()) - set(_python_enums()) - TS_ONLY)
    assert not extra, (
        f"these TypeScript unions have no schema enum behind them, so nothing stops the API "
        f"from emitting a value the client cannot represent: {extra}"
    )


@pytest.mark.parametrize("name", sorted(_python_enums()))
def test_members_match_the_typescript_union(name: str) -> None:
    python_values = [member.value for member in _python_enums()[name]]
    ts_values = _typescript_unions().get(name, [])

    assert sorted(python_values) == sorted(ts_values), (
        f"{name} has drifted: Python emits {sorted(python_values)}, the client accepts "
        f"{sorted(ts_values)}"
    )


@pytest.mark.parametrize("name", sorted(_python_enums()))
def test_members_stringify_to_their_wire_value(name: str) -> None:
    """``f"{member}"`` must be the wire value — see the module docstring."""
    for member in _python_enums()[name]:
        assert str(member) == member.value, (
            f"{name}.{member.name} stringifies as {str(member)!r}, not {member.value!r}; "
            "an f-string carrying it into a log line or payload would emit the Python repr"
        )
        assert f"{member}" == member.value
        assert member == member.value, "comparison against the raw wire string must still hold"


def test_members_are_lower_snake_case() -> None:
    """The client matches on these literally; a stray capital or hyphen is a breaking change."""
    offenders = [
        f"{name}.{member.name} = {member.value!r}"
        for name, enum in _python_enums().items()
        for member in enum
        if not re.fullmatch(r"[a-z][a-z0-9_]*", member.value)
    ]
    assert not offenders, f"non lower_snake_case wire values: {offenders}"
