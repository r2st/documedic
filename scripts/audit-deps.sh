#!/usr/bin/env bash
#
# Check the pinned Python dependencies against the PyPI advisory database.
#
#   scripts/audit-deps.sh            # production dependencies only — what actually ships
#   scripts/audit-deps.sh --dev      # ...plus the test/lint tooling
#   scripts/audit-deps.sh --json     # machine-readable, for CI
#
# This needs the network, which is why it is a script and not a test. The offline half of the
# same job — asserting that the pins established by a security round have not been walked back
# — lives in apps/api/tests/test_dependency_security_floor.py and runs with the suite.
#
# A non-zero exit means pip-audit found something. That is the start of a triage, not an
# automatic upgrade: the question for each finding is whether this application reaches the
# vulnerable code. requirements.txt records that reasoning inline for every pin this round
# moved, and for the ones it deliberately did not.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQUIREMENTS="$ROOT/apps/api/requirements.txt"
# Declared with a placeholder rather than empty: under `set -u`, expanding an empty array with
# `"${arr[@]}"` is an unbound-variable error on bash 3.2, which is the bash macOS ships.
FORMAT_ARGS=(--progress-spinner off)

for arg in "$@"; do
  case "$arg" in
    --dev) REQUIREMENTS="$ROOT/apps/api/requirements-dev.txt" ;;
    --json) FORMAT_ARGS+=(--format json) ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if ! python -m pip_audit --version >/dev/null 2>&1; then
  echo "pip-audit is not installed. Install the dev dependencies:" >&2
  echo "  pip install -r $ROOT/apps/api/requirements-dev.txt" >&2
  exit 2
fi

echo "==> Auditing $(basename "$REQUIREMENTS")"
exec python -m pip_audit -r "$REQUIREMENTS" "${FORMAT_ARGS[@]}"
