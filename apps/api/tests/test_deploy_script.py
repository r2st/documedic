"""The deploy script's behaviour, exercised against stubbed commands.

``deploy.sh`` spent the first eighty-six rounds of this project existing only on the production
host, which made the deploy procedure unreviewable and unreproducible: no clone had it, no
change to it was ever in a diff, and nothing tested it. It is in the repository now, and this
file is what stops it drifting back into a shell script nobody reads.

**How this tests a shell script.** A temporary directory is built that looks like a Documedic
checkout — ``deploy.sh``, ``apps/api/requirements.txt``, ``data/migrations/alembic.ini`` — and a
stub ``bin`` directory is put at the front of ``PATH`` holding fakes for every external command
the script runs: git, npm, npx, pip, alembic, systemctl, curl and sleep. Each stub appends its
own name and arguments to a log file and exits with whatever code the test asked for. The script
then runs for real, and the assertions are about the log: what was invoked, in what order, and
what was *not* invoked.

That last one is the point of most of these tests. The property that matters about a deploy is
usually something it declined to do — restart a service after the migration failed, or claim
success when the API never came back — and a property of that shape can only be tested by
running the thing and watching what it did not touch.

The stub for ``sleep`` is a no-op, which is what makes running the whole script eight times in a
unit-test suite take under a second rather than the seventy-odd seconds of real waiting.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

DEPLOY_SCRIPT = Path(__file__).resolve().parents[3] / "deploy.sh"

# Every external command deploy.sh reaches for. A stub is generated for each; the test decides
# which of them fail. Missing one from this list does not silently pass -- the real binary would
# be found on PATH instead and the test would start talking to the developer's machine -- so the
# roster is asserted against the script's own text by ``test_no_uncovered_external_commands``.
STUBBED_COMMANDS = ("git", "npm", "npx", "pip", "alembic", "systemctl", "curl", "sleep")

# What each stub prints or returns when the test does not override it. ``git rev-parse`` has to
# produce a plausible sha because the script slices it (``${AFTER:0:7}``) and compares before to
# after; ``systemctl is-active`` has to say "active" or the verification step fails.
_STUB_TEMPLATE = """#!/usr/bin/env bash
echo "{name} $*" >> "$DEPLOY_TEST_LOG"
{body}
exit ${{DEPLOY_TEST_{upper}_EXIT:-0}}
"""

_STUB_BODIES = {
    "git": (
        'case "$1" in\n'
        '  rev-parse) echo "0123456789abcdef0123456789abcdef01234567" ;;\n'
        "  log) : ;;\n"
        "esac"
    ),
    "systemctl": 'case "$1" in is-active) echo "${DEPLOY_TEST_UNIT_STATE:-active}" ;; esac',
    "sleep": ":",
}


@pytest.fixture
def deploy_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A throwaway checkout plus a PATH of stubs, ready to run ``deploy.sh`` against."""
    project = tmp_path / "checkout"
    (project / "apps" / "api").mkdir(parents=True)
    (project / "apps" / "api" / "requirements.txt").write_text("fastapi\n")
    (project / "data" / "migrations").mkdir(parents=True)
    (project / "data" / "migrations" / "alembic.ini").write_text("[alembic]\n")
    (project / "deploy.sh").write_bytes(DEPLOY_SCRIPT.read_bytes())
    (project / "deploy.sh").chmod(0o755)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for name in STUBBED_COMMANDS:
        stub = stub_bin / name
        stub.write_text(
            _STUB_TEMPLATE.format(name=name, upper=name.upper(), body=_STUB_BODIES.get(name, ":"))
        )
        stub.chmod(0o755)

    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        # A deliberately minimal environment. Inheriting the developer's PATH would let a real
        # systemctl or curl through if a stub were ever missing, and this suite must never be
        # able to restart anything.
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DEPLOY_TEST_LOG": str(log),
        # No .venv in the throwaway checkout, so the script's pip_bin/alembic_bin helpers fall
        # through to PATH -- which is where the stubs are. That fallback exists for this.
        "DOCUMEDIC_DIR": str(project),
    }
    return project, env


def _run(project: Path, env: dict[str, str], **overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(project / "deploy.sh")],
        env={**env, **overrides},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(env: dict[str, str]) -> list[str]:
    return Path(env["DEPLOY_TEST_LOG"]).read_text().splitlines()


# --- The script itself -------------------------------------------------------------------


def test_deploy_script_is_committed_and_executable() -> None:
    """The whole point of the round: the deploy procedure is in the repository, not on a host."""
    assert DEPLOY_SCRIPT.is_file()
    assert os.access(DEPLOY_SCRIPT, os.X_OK), "deploy.sh must be committed with the exec bit set"


def test_deploy_script_parses() -> None:
    result = subprocess.run(["bash", "-n", str(DEPLOY_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_project_dir_is_not_hardcoded(deploy_env: tuple[Path, dict[str, str]]) -> None:
    """A fresh clone at any path deploys itself.

    The version that lived on the server pinned ``PROJECT_DIR=/opt/documedic``, so the file was
    not a deploy procedure so much as a description of one particular machine. Here it runs
    entirely inside a tmp_path and never mentions /opt.
    """
    project, env = deploy_env
    result = _run(project, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(project) in result.stdout
    assert "/opt/documedic" not in result.stdout


def test_default_dir_is_the_scripts_own_directory(
    deploy_env: tuple[Path, dict[str, str]],
) -> None:
    """With no DOCUMEDIC_DIR set, the script deploys the checkout it is part of."""
    project, env = deploy_env
    env = {k: v for k, v in env.items() if k != "DOCUMEDIC_DIR"}
    result = _run(project, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(project) in result.stdout


# --- The happy path ----------------------------------------------------------------------


def test_successful_deploy_runs_every_step_in_order(
    deploy_env: tuple[Path, dict[str, str]],
) -> None:
    project, env = deploy_env
    result = _run(project, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Deploy complete!" in result.stdout

    calls = _calls(env)
    joined = "\n".join(calls)
    assert "git fetch origin" in joined
    assert any(c.startswith("pip install") for c in calls)
    assert "npm install" in joined
    assert any("alembic upgrade head" in c for c in calls)
    assert any(c.startswith("npx turbo build") for c in calls)
    assert "systemctl restart documedic" in calls
    assert "systemctl restart documedic-web" in calls


def test_migrations_run_before_the_restart(deploy_env: tuple[Path, dict[str, str]]) -> None:
    """Ordering is the safety property, not a preference.

    Restarting first would mean the new code is serving traffic while the schema it needs is
    still being applied, and a migration that then fails leaves a running release that cannot
    answer requests. Migrating first means a schema that will not move stops the deploy with the
    previous version still up.
    """
    project, env = deploy_env
    _run(project, env)
    calls = _calls(env)
    migrate_at = next(i for i, c in enumerate(calls) if "alembic upgrade head" in c)
    restart_at = next(i for i, c in enumerate(calls) if c.startswith("systemctl restart"))
    assert migrate_at < restart_at


def test_service_names_and_ports_are_overridable(
    deploy_env: tuple[Path, dict[str, str]],
) -> None:
    """A staging host with different unit names does not need a forked copy of this file."""
    project, env = deploy_env
    result = _run(
        project,
        env,
        DOCUMEDIC_API_SERVICE="stage-api",
        DOCUMEDIC_WEB_SERVICE="stage-web",
        DOCUMEDIC_API_PORT="9001",
        DOCUMEDIC_WEB_PORT="9002",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(env)
    assert "systemctl restart stage-api" in calls
    assert "systemctl restart stage-web" in calls
    assert any("localhost:9001/health" in c for c in calls)
    assert any("localhost:9002/" in c for c in calls)


# --- The failures that must stop the deploy ----------------------------------------------


def test_failed_migration_aborts_before_anything_restarts(
    deploy_env: tuple[Path, dict[str, str]],
) -> None:
    """The defect this round fixed.

    A migration failure used to print a warning and carry on, so the services came back running
    new code against an old schema and the deploy said "complete". Now it exits non-zero with
    nothing restarted, which leaves the previous release serving — the correct outcome of a
    schema that will not apply.
    """
    project, env = deploy_env
    result = _run(project, env, DEPLOY_TEST_ALEMBIC_EXIT="1")

    assert result.returncode == 1
    assert "aborting deploy before restart" in result.stdout
    assert "Deploy complete!" not in result.stdout

    calls = _calls(env)
    assert not any(c.startswith("systemctl restart") for c in calls), (
        "a failed migration must not restart anything"
    )
    # And it stops before the build too, so a broken release does not even get compiled onto
    # the host.
    assert not any(c.startswith("npx turbo build") for c in calls)


def test_failed_git_fetch_aborts(deploy_env: tuple[Path, dict[str, str]]) -> None:
    project, env = deploy_env
    result = _run(project, env, DEPLOY_TEST_GIT_EXIT="1")
    assert result.returncode == 1
    assert not any(c.startswith("systemctl restart") for c in _calls(env))


def test_dead_service_is_a_failure_not_a_warning(
    deploy_env: tuple[Path, dict[str, str]],
) -> None:
    """The old script always exited 0.

    Which meant a caller — a CI step, a wrapper, an operator's ``&& echo ok`` — could not tell a
    clean deploy from one that came back with a dead frontend.
    """
    project, env = deploy_env
    result = _run(project, env, DEPLOY_TEST_UNIT_STATE="failed")
    assert result.returncode == 1
    assert "Deploy finished with" in result.stdout


def test_unresponsive_api_is_a_failure(deploy_env: tuple[Path, dict[str, str]]) -> None:
    project, env = deploy_env
    result = _run(project, env, DEPLOY_TEST_CURL_EXIT="1")
    assert result.returncode == 1
    assert "API not responding" in result.stdout
    assert "Frontend not responding" in result.stdout


def test_health_probe_asks_for_health_and_nothing_else(
    deploy_env: tuple[Path, dict[str, str]],
) -> None:
    """The API is up when ``/health`` says so, not when something answers the port.

    The earlier version also accepted a 2xx from ``/`` as proof, which any process bound to the
    port satisfies — including a previous release that failed to stop.
    """
    project, env = deploy_env
    _run(project, env)
    api_probes = [c for c in _calls(env) if c.startswith("curl") and ":3003" in c]
    assert api_probes
    assert all("/health" in c for c in api_probes)


def test_no_uncovered_external_commands() -> None:
    """Every command the script shells out to has a stub, so no test can reach the real one.

    Without this, adding a ``docker compose`` line to deploy.sh tomorrow would leave the suite
    quietly running the developer's actual docker.
    """
    text = DEPLOY_SCRIPT.read_text()
    # The commands worth guarding are the ones that touch the world outside the checkout.
    for command in ("systemctl", "curl", "npm", "npx", "git", "alembic", "pip", "docker"):
        if f"{command} " in text and command not in STUBBED_COMMANDS:
            pytest.fail(f"deploy.sh invokes {command!r} but no stub exists for it")
