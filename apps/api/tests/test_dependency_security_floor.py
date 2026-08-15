"""The security floors this round established, pinned so a later edit cannot walk one back.

``scripts/audit-deps.sh`` is the live check, and it needs the network — so it is a script, run
deliberately, and it tells you about advisories published after today. This file is the
offline half: for each dependency that sits on the untrusted-input path, it asserts the
minimum version whose reasoning is written into ``requirements.txt``.

The failure mode being guarded against is mundane and common. Someone resolves a conflict, or
copies a pin from an older branch, or reverts a merge, and a version floor set for a security
reason quietly drops — with nothing failing, because the reason was a comment. A comment is
not a check.

What this file is *not*: a substitute for running the audit. It knows only about the packages
and versions named here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"
DEV_REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements-dev.txt"
DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"

# package -> (minimum version, why this floor exists)
#
# Only packages this application can reach with attacker-influenced input. A floor on
# something the API never feeds untrusted data to would be noise, and noise is what makes a
# list like this get deleted.
SECURITY_FLOORS: dict[str, tuple[tuple[int, ...], str]] = {
    "pypdf": (
        (6, 15, 0),
        "parses clinician-uploaded PDFs inline in the upload request; 5.1.0 carried 37 "
        "advisories, all of the form 'a crafted PDF causes an infinite loop or exhausts RAM'",
    ),
    "python-multipart": (
        (0, 0, 31),
        "the multipart parser for the document upload route; 0.0.20 had three separate "
        "denial-of-service advisories in it (PYSEC-2026-3038, -3039, -3040)",
    ),
    "starlette": (
        (0, 49, 1),
        "multipart-with-large-files DoS (PYSEC-2026-1941) and quadratic Range header parsing "
        "in FileResponse (PYSEC-2026-1942)",
    ),
    "pyjwt": (
        (2, 13, 0),
        "verifier-side algorithm allow-list bypass in jwt.decode (PYSEC-2026-176) — the "
        "function that authenticates every request to this API",
    ),
    "cryptography": (
        (48, 0, 1),
        "the wheels statically link OpenSSL, and every copy before 48.0.1 is vulnerable "
        "(PYSEC-2026-1284, GHSA-537c-gmf6-5ccf); this library encrypts patient PII at rest",
    ),
}

# Nothing imports these, and each one carried advisories into the production image for no
# running code. Removing an unused dependency is the cheapest security fix available; a test
# is what stops it drifting back in on a merge.
FORBIDDEN_DEPENDENCIES = {
    "langgraph": (
        "nothing imports it — the 8-agent orchestrator in app/agents/ is hand-written. "
        "Pinning it pulled langgraph, langgraph-checkpoint, langgraph-sdk and langchain-core "
        "into the image: ten advisories, three of them RCE through checkpoint "
        "deserialization, to run no code at all."
    ),
}

_PIN = re.compile(r"\A([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(.+)\Z")


def _pins(path: Path) -> dict[str, str]:
    """Every ``name==version`` in a requirements file, normalised to lowercase names."""
    found = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        match = _PIN.match(line)
        if match:
            found[match.group(1).lower().replace("_", "-")] = match.group(2).strip()
    return found


def _version(raw: str) -> tuple[int, ...]:
    """``"6.16.1"`` -> ``(6, 16, 1)``. Trailing non-numeric segments (``.post0``, ``rc1``) are
    dropped, which is safe for a floor: it can only make the parsed version compare lower."""
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = re.match(r"\d+", chunk)
        if digits is None:
            break
        parts.append(int(digits.group()))
    return tuple(parts)


@pytest.mark.parametrize("package", sorted(SECURITY_FLOORS))
def test_the_pin_is_at_or_above_its_security_floor(package):
    floor, why = SECURITY_FLOORS[package]
    pins = _pins(REQUIREMENTS)
    assert package in pins, f"{package} is no longer pinned in requirements.txt — {why}"
    pinned = _version(pins[package])
    assert pinned >= floor, (
        f"{package}=={pins[package]} is below the security floor "
        f"{'.'.join(str(n) for n in floor)}: {why}"
    )


@pytest.mark.parametrize("package", sorted(FORBIDDEN_DEPENDENCIES))
def test_a_removed_dependency_has_not_come_back(package):
    why = FORBIDDEN_DEPENDENCIES[package]
    assert package not in _pins(REQUIREMENTS), f"{package} is pinned again: {why}"


def test_nothing_imports_the_removed_dependencies():
    """The pin is only half of it. If code starts importing langgraph, the pin has to come
    back — so this asserts the reason the pin was removable is still true, rather than leaving
    the two checks able to disagree."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        path.relative_to(app_dir)
        for path in app_dir.rglob("*.py")
        for package in FORBIDDEN_DEPENDENCIES
        if re.search(rf"^\s*(?:from|import)\s+{package}\b", path.read_text(), re.MULTILINE)
    ]
    assert not offenders, f"these modules import a removed dependency: {offenders}"


# --- The production image carries production dependencies and nothing else ------------------


def test_the_test_tooling_is_not_in_the_production_requirements():
    """The Dockerfile installs requirements.txt whole, so anything in it ships. pytest, ruff
    and mypy were all being built into a container that runs uvicorn — image weight and a
    wider surface for no purpose, pytest's own advisory included."""
    production = _pins(REQUIREMENTS)
    for tool in ("pytest", "ruff", "mypy", "pytest-cov", "pytest-asyncio", "pip-audit"):
        assert tool not in production, (
            f"{tool} is in requirements.txt, so it ships in the production image. "
            "Dev tooling belongs in requirements-dev.txt."
        )


def test_the_dev_requirements_include_the_production_ones():
    """...and the split must not mean a developer has to remember two commands."""
    assert "-r requirements.txt" in DEV_REQUIREMENTS.read_text()


def test_the_dockerfile_installs_only_the_production_requirements():
    """The half of the split that actually keeps the tooling out of the image."""
    dockerfile = DOCKERFILE.read_text()
    assert "requirements.txt" in dockerfile
    assert "requirements-dev.txt" not in dockerfile


def test_the_test_tooling_is_still_installed_somewhere():
    """Guards the obvious way to make the test above pass: deleting the tools."""
    dev = _pins(DEV_REQUIREMENTS)
    for tool in ("pytest", "ruff", "mypy", "pip-audit"):
        assert tool in dev, f"{tool} is not pinned anywhere"


# --- The advisories left open, and the reason each one has no sink here ---------------------
#
# `scripts/audit-deps.sh` still reports starlette after this round: the remaining findings are
# fixed only in the 1.x line, which is a major release and a round of its own. The argument for
# not taking it now is that none of the vulnerable code is reachable from this application.
# That argument is only worth anything if it stays true, so it is a test rather than a comment.

# Starlette API -> the advisory that makes touching it a decision, not an accident.
UNREACHABLE_STARLETTE_SINKS = {
    r"request\.url\b": "PYSEC-2026-161 / -248: Host and path are not validated before "
    "`request.url` is reconstructed. Reading it here would give those a sink.",
    r"request\.base_url\b": "PYSEC-2026-161: same reconstruction, same unvalidated Host.",
    r"\bHTTPEndpoint\b": "PYSEC-2026-2280: HTTPEndpoint picks its handler by lowercasing the "
    "request method. This API uses function routes.",
    r"\bStaticFiles\b": "PYSEC-2026-2281: StaticFiles follows UNC paths on Windows. This API "
    "serves no static files.",
}


@pytest.mark.parametrize("pattern", sorted(UNREACHABLE_STARLETTE_SINKS))
def test_the_open_starlette_advisories_still_have_no_sink(pattern):
    """If this fails, the code grew a use of the vulnerable API — so the reasoning in
    requirements.txt no longer holds and starlette has to move to 1.x."""
    why = UNREACHABLE_STARLETTE_SINKS[pattern]
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        # Comments included deliberately: this is about whether the name appears at all, and a
        # commented-out call is a call someone is about to uncomment.
        if re.search(pattern, path.read_text())
    ]
    assert not offenders, f"{offenders} now reach an API with an open advisory. {why}"


def test_the_multipart_route_bounds_its_own_body():
    """PYSEC-2026-249 is about `form()`'s own `max_fields`/`max_part_size` bounds. It matters
    less here because the byte ceiling is applied before Starlette sees the body at all — this
    asserts that ordering is still in place, since it is half the reason the advisory is
    survivable."""
    from app.main import create_app
    from app.middleware import RequestBodyLimitMiddleware

    stack = create_app().user_middleware
    classes = [entry.cls for entry in stack]
    assert RequestBodyLimitMiddleware in classes
    # Added first => innermost, so it sees the body before the router hands it to `form()`.
    assert classes.index(RequestBodyLimitMiddleware) == len(classes) - 1


def test_ruff_and_mypy_configuration_still_points_at_this_project():
    """A sanity check on the split: the lint/type config lives in pyproject.toml, which is not
    what moved, and the tools that read it are now installed from a different file."""
    config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert config["tool"]["ruff"]["src"] == ["app", "tests"]
    assert config["tool"]["mypy"]["disallow_untyped_defs"] is True
