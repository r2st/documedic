"""Critical Safety Rule #8, as a property of the module graph rather than as a convention.

"Allergy cross-checks, contraindication detection and drug interaction alerts must work when the
network is down" is asserted in several places in this suite by simulating an outage and checking
that a specific finding still appears — the renal hard block, in ``test_chaos_resilience``. That
is a good test of the path it exercises and a weak guarantee about the rule, because it can only
speak for the checks it happens to name. Two checks were added to ``evaluate_drug_safety`` in
this round alone, and neither is covered by the outage test that predates them.

What actually makes the rule true is structural: ``app.core.safety`` and ``app.core.lab_safety``
import nothing that can reach a database, a network, a file or a clock. Every check is a pure
function over dataclasses the service layer hands it, which is why the same logic can be ported
to run client-side offline and why an LLM outage cannot take a hard block with it.

That is easy to break silently. Importing ``settings`` for a threshold, or a model class for a
type annotation, or ``httpx`` to fetch a code list, would each read as an ordinary tidy-up in
review and would each end with the deterministic core no more available than the model it backs
up. So the graph is pinned here, and the pin fails at the import that breaks it rather than at
the incident.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_APP = pathlib.Path(__file__).resolve().parents[1] / "app"

# The modules Critical Safety Rule #8 is about: the deterministic drug-safety engine, the
# deterministic lab critical-value screen, and the two computation modules they rest on.
_OFFLINE_ROOTS = (
    "app.core.safety",
    "app.core.lab_safety",
    "app.core.clinical",
    "app.core.hepatic",
    # Reached from ``app.core.safety`` for the two checks whose subject is a model-written
    # recommendation rather than the patient. It belongs under this rule for the same reason the
    # rest do: a drug name that resolves to nothing and a dose a thousand times its own strength
    # are exactly the failures that must still be caught when the network is down — the provider
    # that wrote them may be the thing that is failing.
    "app.core.dose_text",
    # The curated therapeutic ranges, reached from ``app.core.safety`` by ``check_dose_ranges``.
    # A per-drug maximum is the shape of thing that most wants to become configuration — a table
    # of numbers someone will eventually want to edit without a deploy — and the moment it does,
    # the dose check acquires a file read and a parse failure. It is a Python literal reached
    # only through ``re``, ``dataclasses`` and ``app.core.dose_text``, and this line is what
    # keeps it that way.
    "app.core.dose_range",
    # The longitudinal trend engine. Not a check that blocks anything, and here for the other
    # half of Rule #8 — "core record access ... must work offline". Reading which way a
    # creatinine is going is exactly what a clinician still needs when the reasoning engine is
    # unavailable, and the module's whole correctness argument is that it converts units through
    # ``app.core.lab_safety`` rather than plotting the column: a threshold pulled from settings,
    # or a conversion table fetched at import, would put that behind the same availability the
    # LLM has.
    "app.core.lab_trend",
)

# Everything the standard library gives these modules today. An addition here is not forbidden,
# but it is a decision: the list exists so that adding one is a line in a diff rather than an
# invisible widening. ``datetime`` is here for date arithmetic on values passed in, not for
# reading the clock — see the wall-clock test below.
_ALLOWED_STDLIB = frozenset(
    {
        "__future__",
        "dataclasses",
        "datetime",
        "decimal",
        "functools",
        "math",
        "re",
        "typing",
    }
)


def _module_path(module: str) -> pathlib.Path | None:
    path = _APP.parent / (module.replace(".", "/") + ".py")
    return path if path.exists() else None


def _direct_imports(path: pathlib.Path) -> set[str]:
    """Top-level module names imported by one file, absolute imports only.

    ``from . import x`` would be a relative import; this package uses absolute imports
    throughout (a code convention in CLAUDE.md), so a relative one appearing here would be
    invisible to this walk — which is what the convention test at the bottom is for.
    """
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _transitive_imports() -> tuple[set[str], set[str]]:
    """Every module reachable from the offline roots, split into first-party and everything else."""
    seen: set[str] = set()
    stack: list[str] = list(_OFFLINE_ROOTS)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_path(module)
        if path is not None:
            stack.extend(_direct_imports(path))
    return (
        {m for m in seen if m.startswith("app.")},
        {m for m in seen if not m.startswith("app.")},
    )


def test_the_offline_engine_reaches_nothing_but_the_standard_library():
    """The whole rule in one assertion. Anything outside this set can fail, block or need a
    connection, and a check that can do any of those is not an offline check."""
    _first_party, external = _transitive_imports()
    assert external - _ALLOWED_STDLIB == set()


def test_the_offline_engine_reaches_no_other_part_of_this_application():
    """No service, no model, no router, no config. ``settings`` is the tempting one — a curated
    threshold looks like configuration — and it carries an environment, a file read and a
    validation failure into a code path whose entire value is that it cannot fail."""
    first_party, _external = _transitive_imports()
    assert first_party == set(_OFFLINE_ROOTS)


@pytest.mark.parametrize(
    "forbidden",
    [
        "sqlalchemy",
        "httpx",
        "anthropic",
        "openai",
        "redis",
        "fastapi",
        "pydantic",
        "app.config",
        "app.db",
        "app.models",
        "app.services",
        "app.agents",
    ],
)
def test_a_named_source_of_failure_is_absent(forbidden):
    """Named individually as well as excluded in bulk, so a failure says which door opened."""
    first_party, external = _transitive_imports()
    reachable = first_party | external
    assert not any(m == forbidden or m.startswith(forbidden + ".") for m in reachable)


def test_the_engine_never_reads_the_wall_clock():
    """A pure function of its arguments, which is what makes it reproducible in an audit.

    ``datetime`` is imported for arithmetic on dates the caller passes in. Reading *now* inside
    a check would make the same context produce different flags on different days, so a
    persisted ``ClinicalSuggestion`` could no longer be reproduced from the record it was made
    against — and those records are immutable (Critical Safety Rule #7) precisely so they can be.
    The patient's age is derived in the service layer, where the clock legitimately lives, and
    handed in as a number.
    """
    offenders: list[str] = []
    for module in _OFFLINE_ROOTS:
        path = _module_path(module)
        assert path is not None, module
        source = path.read_text()
        for call in ("datetime.now(", "date.today(", "time.time("):
            if call in source:
                offenders.append(f"{module}: {call}")
    assert offenders == []


def test_every_check_wired_into_the_evaluation_lives_in_the_pure_module():
    """The list the tests above are only as good as. A check added to ``evaluate_drug_safety``
    from a module outside this graph would inherit none of the guarantees above, and would
    degrade the whole evaluation to the availability of whatever it imported.
    """
    from app.core import safety

    source = (_APP / "core" / "safety.py").read_text()
    body = source.split("def evaluate_drug_safety(")[1]
    called = {
        line.split("flags.extend(")[1].split("(")[0]
        for line in body.splitlines()
        if "flags.extend(" in line
    }

    assert called, "the evaluation's check list could not be read"
    for name in called:
        assert hasattr(safety, name), name
        assert getattr(safety, name).__module__ == "app.core.safety", name
