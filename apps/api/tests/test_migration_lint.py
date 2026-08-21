"""The migration tree is held to the same lint rules as the rest of the project.

Ruff resolves its configuration by walking up from each file, and the repository root has no
``pyproject.toml`` — the backend's lives at ``apps/api/pyproject.toml``, which is not an ancestor
of ``data/migrations``. So a bare ``ruff check data/migrations`` was linting the schema history
against ruff's built-in defaults rather than the project's rules, and nobody ever cleaned up the
result because the result was not the project's opinion. ``data/migrations/ruff.toml`` fixes
that; this file is what keeps it fixed.

Two things are pinned:

* the migration tree passes ``ruff check`` and ``ruff format --check`` cleanly, so a new
  migration cannot land un-formatted or with an unsorted import block;
* ``script.py.mako`` — the template every new migration is generated from — itself produces code
  that satisfies those rules. Alembic's stock template does not: it writes
  ``Union[str, None]`` and imports ``Sequence`` from ``typing``, both of which the project's
  ``UP`` rules reject. Without this second assertion the tree would be clean today and dirty
  again on the next ``alembic revision``, which is how it got to 193 findings in the first place.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO_ROOT / "data" / "migrations"


def _find_ruff() -> str | None:
    """The interpreter's own ruff before whatever is first on PATH.

    requirements-dev.txt pins a ruff version; a different one earlier on the developer's PATH
    (a conda base environment, a homebrew install) has different rules and different formatting,
    and a lint contract asserted against an unpinned linter is not a contract.
    """
    local = Path(sys.executable).parent / "ruff"
    return str(local) if local.is_file() else shutil.which("ruff")


RUFF = _find_ruff()

pytestmark = pytest.mark.skipif(RUFF is None, reason="ruff is not on PATH")


def _ruff(*args: str) -> subprocess.CompletedProcess[str]:
    assert RUFF is not None
    return subprocess.run([RUFF, *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)


def test_migrations_have_their_own_ruff_config() -> None:
    """Without this file the tree is linted against ruff's defaults, not the project's."""
    config = MIGRATIONS / "ruff.toml"
    assert config.is_file()
    text = config.read_text()
    # The two settings the rest of the project is held to. Stated as an assertion because the
    # whole point of the file is that the migration tree and apps/api agree; a copy that quietly
    # drifted to a different line length would be worse than no copy at all.
    assert "line-length = 100" in text
    assert '"UP"' in text


def test_migration_tree_passes_ruff_check() -> None:
    result = _ruff("check", str(MIGRATIONS))
    assert result.returncode == 0, result.stdout + result.stderr


def test_migration_tree_is_formatted() -> None:
    result = _ruff("format", "--check", str(MIGRATIONS))
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_migrations_would_pass(tmp_path: Path) -> None:
    """The template is the thing that has to be clean, not just today's files.

    Alembic renders ``script.py.mako`` for every new revision, so a template carrying
    ``Union[str, None]`` and ``from typing import Sequence`` re-introduces UP007 and UP035 on the
    next migration anybody writes. Rendered here with the placeholders filled the way alembic
    fills them, and linted as a migration would be.
    """
    template = (MIGRATIONS / "script.py.mako").read_text()
    # Bodies that use ``op`` and ``sa``, because a generated migration does. Rendering the
    # ``pass`` placeholders instead would leave both imports unused and the probe would fail on
    # F401 — a property of the empty rendering, not of the template.
    upgrade_body = (
        'op.add_column("patients", sa.Column("probe", sa.String(length=8), nullable=True))'
    )
    downgrade_body = 'op.drop_column("patients", "probe")'
    rendered = (
        template.replace("${message}", "a new migration")
        .replace("${up_revision}", "0099")
        .replace("${down_revision | comma,n}", "0098")
        .replace("${create_date}", "2026-08-21 00:00:00.000000")
        .replace('${imports if imports else ""}', "")
        .replace("${repr(up_revision)}", '"0099"')
        .replace("${repr(down_revision)}", '"0098"')
        .replace("${repr(branch_labels)}", "None")
        .replace("${repr(depends_on)}", "None")
        .replace('${upgrades if upgrades else "pass"}', upgrade_body)
        .replace('${downgrades if downgrades else "pass"}', downgrade_body)
    )
    assert "${" not in rendered, f"unrendered placeholder left in template:\n{rendered}"

    # Laid out as ``<root>/ruff.toml`` + ``<root>/versions/<file>``, mirroring the real tree, so
    # the config ruff discovers by walking up from the probe is the migrations' own — including
    # the ``versions/*`` per-file-ignores, which are relative to the config's directory. Built in
    # tmp_path rather than written into the live versions/ directory: a probe file sitting in the
    # real one, however briefly, is a file alembic would try to load as a revision.
    (tmp_path / "ruff.toml").write_text((MIGRATIONS / "ruff.toml").read_text())
    (tmp_path / "versions").mkdir()
    probe = tmp_path / "versions" / "0099_probe.py"
    probe.write_text(rendered)

    check = _ruff("check", str(probe))
    assert check.returncode == 0, check.stdout + check.stderr
    fmt = _ruff("format", "--check", str(probe))
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr
