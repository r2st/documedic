#!/usr/bin/env bash
# =============================================================================
# Documedic Deploy Script
# Pulls latest from origin/main, installs deps, migrates, builds, restarts.
# Usage: cd /opt/documedic && ./deploy.sh
# =============================================================================
#
# This script lived only on the production host for the first eighty-six rounds of work, which
# meant the deploy procedure was a file one server held and no clone could reproduce. It is in
# the repository now, and the two changes that made it safe to put here are worth stating
# because both are corrections rather than ports:
#
# **Nothing is hardcoded to /opt/documedic.** The project directory is the directory the script
# is in, so a fresh clone anywhere deploys itself. ``DOCUMEDIC_DIR`` overrides it, and every
# external command (git, npm, alembic, systemctl, curl) is resolved through PATH so the
# behaviour of this file can be exercised by tests/test_deploy_script.py against stubs rather
# than only against the live server.
#
# **A failed migration aborts the deploy before anything restarts.** It used to print a warning
# and carry on, which is the more dangerous of the two possible mistakes: the services come back
# running new code against an old schema, and the deploy reports success. A migration that did
# not reach head means the release cannot be served, so the old release keeps serving it and a
# human looks at the error. This is the one step whose failure is fatal by design.
#
# Ordering matters for the same reason: migrations run *before* the restart, so a schema that
# refuses to move leaves the previous version untouched and serving.
set -uo pipefail

# The directory this script is in, unless told otherwise. ``cd``-and-``pwd`` rather than
# ``dirname`` alone so the value is absolute however the script was invoked -- the migration and
# build steps below cd around, and a relative PROJECT_DIR would resolve differently in each.
DEFAULT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${DOCUMEDIC_DIR:-$DEFAULT_DIR}"
VENV_DIR="${DOCUMEDIC_VENV:-${PROJECT_DIR}/.venv}"
API_DIR="${PROJECT_DIR}/apps/api"
MIGRATIONS_DIR="${PROJECT_DIR}/data/migrations"

# The two systemd units and the ports they listen on. Overridable so a staging host running the
# same code under different unit names does not need a forked copy of this file.
API_SERVICE="${DOCUMEDIC_API_SERVICE:-documedic}"
WEB_SERVICE="${DOCUMEDIC_WEB_SERVICE:-documedic-web}"
API_PORT="${DOCUMEDIC_API_PORT:-3003}"
WEB_PORT="${DOCUMEDIC_WEB_PORT:-3004}"

# Prefer the project virtualenv's tools when it has them, fall back to PATH when it does not.
# The fallback is what lets the test suite run this script under stubs; on the server the venv
# always exists and always wins.
pip_bin()     { if [ -x "${VENV_DIR}/bin/pip" ]; then echo "${VENV_DIR}/bin/pip"; else echo "pip"; fi; }
alembic_bin() { if [ -x "${VENV_DIR}/bin/alembic" ]; then echo "${VENV_DIR}/bin/alembic"; else echo "alembic"; fi; }

RED="\033[0;31m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
BLUE="\033[0;34m"
NC="\033[0m"

step() { echo -e "\n${BLUE}▸ $1${NC}"; }
ok()   { echo -e "${GREEN}  ✓ $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }
fail() { echo -e "${RED}  ✗ $1${NC}"; }

TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S %Z")
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  Documedic Deploy — ${TIMESTAMP}${NC}"
echo -e "${BLUE}  ${PROJECT_DIR}${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

cd "$PROJECT_DIR" || { fail "no such directory: ${PROJECT_DIR}"; exit 1; }
ERRORS=0

# --- 1. Pull latest code ---
step "Pulling latest from origin/main..."
BEFORE=$(git rev-parse HEAD)
git fetch origin || { fail "git fetch failed"; exit 1; }
git reset --hard origin/main || { fail "git reset failed"; exit 1; }
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    ok "Already up to date (${AFTER:0:7})"
else
    COMMITS=$(git log --oneline "${BEFORE}..${AFTER}" | wc -l)
    ok "Updated ${BEFORE:0:7} → ${AFTER:0:7} (${COMMITS} new commits)"
    echo ""
    git log --oneline "${BEFORE}..${AFTER}" | head -10
fi

# --- 2. Install backend dependencies ---
step "Installing backend Python dependencies..."
if [ -f "${API_DIR}/requirements.txt" ]; then
    if "$(pip_bin)" install -q -r "${API_DIR}/requirements.txt" 2>&1 | tail -3; then
        ok "Backend dependencies installed"
    else
        warn "pip install had warnings (may be OK)"
    fi
else
    warn "No requirements.txt found — skipping"
fi

# --- 3. Install frontend dependencies ---
step "Installing frontend Node dependencies..."
cd "$PROJECT_DIR"
npm install 2>&1 | tail -5
ok "Frontend dependencies installed"

# --- 4. Run database migrations ---
#
# Before the build and before the restart, and fatal on failure. A release whose schema did not
# land cannot be served, and the failure mode this ordering rules out is the expensive one: new
# code restarted against an old schema, reporting a successful deploy while every request that
# touches the new columns 500s. Leaving the previous version running is the correct outcome of a
# migration that will not apply.
step "Running Alembic migrations..."
if [ -f "${MIGRATIONS_DIR}/alembic.ini" ]; then
    cd "$MIGRATIONS_DIR"
    if PYTHONPATH="${API_DIR}" "$(alembic_bin)" upgrade head 2>&1; then
        ok "Migrations complete"
    else
        fail "Migration failed — aborting deploy before restart"
        fail "Services are untouched and still serving ${BEFORE:0:7}"
        echo "    Inspect: cd ${MIGRATIONS_DIR} && PYTHONPATH=${API_DIR} $(alembic_bin) current"
        exit 1
    fi
    cd "$PROJECT_DIR"
else
    warn "No alembic.ini found — skipping migrations"
fi

# --- 5. Build frontend ---
step "Building frontend (Next.js)..."
cd "$PROJECT_DIR"
if npx turbo build 2>&1 | tail -10; then
    ok "Frontend built successfully"
else
    fail "Frontend build failed"
    ERRORS=$((ERRORS + 1))
fi

# --- 6. Restart services ---
step "Restarting services..."
cd "$PROJECT_DIR"
systemctl restart "$API_SERVICE"
sleep 2
systemctl restart "$WEB_SERVICE"
sleep 3
ok "Services restarted"

# --- 7. Verify ---
step "Verifying services..."

API_STATUS=$(systemctl is-active "$API_SERVICE" 2>/dev/null)
if [ "$API_STATUS" = "active" ]; then
    ok "${API_SERVICE}.service (API) — active"
else
    fail "${API_SERVICE}.service (API) — ${API_STATUS}"
    echo "    Logs: journalctl -u ${API_SERVICE} -n 20 --no-pager"
    ERRORS=$((ERRORS + 1))
fi

WEB_STATUS=$(systemctl is-active "$WEB_SERVICE" 2>/dev/null)
if [ "$WEB_STATUS" = "active" ]; then
    ok "${WEB_SERVICE}.service (Frontend) — active"
else
    fail "${WEB_SERVICE}.service (Frontend) — ${WEB_STATUS}"
    echo "    Logs: journalctl -u ${WEB_SERVICE} -n 20 --no-pager"
    ERRORS=$((ERRORS + 1))
fi

# Health checks with retries.
#
# ``/health`` and nothing else. The earlier version also accepted a 2xx from ``/`` as proof the
# API was up, which asks a weaker question than the one being asked: any process bound to the
# port answers it, including the previous release that failed to stop. /health is the unauthed
# readiness probe and is the only URL whose 200 means this build is serving.
sleep 2
API_OK=0
for _ in 1 2 3; do
    if curl -sf "http://localhost:${API_PORT}/health" > /dev/null 2>&1; then
        API_OK=1
        break
    fi
    sleep 2
done
if [ "$API_OK" -eq 1 ]; then
    ok "API responding (port ${API_PORT})"
else
    fail "API not responding on port ${API_PORT} — check logs"
    ERRORS=$((ERRORS + 1))
fi

WEB_OK=0
for _ in 1 2 3; do
    if curl -sf "http://localhost:${WEB_PORT}/" > /dev/null 2>&1; then
        WEB_OK=1
        break
    fi
    sleep 2
done
if [ "$WEB_OK" -eq 1 ]; then
    ok "Frontend responding (port ${WEB_PORT})"
else
    fail "Frontend not responding on port ${WEB_PORT} — check logs"
    ERRORS=$((ERRORS + 1))
fi

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
if [ "$ERRORS" -eq 0 ]; then
    echo -e "${GREEN}  Deploy complete! Commit: $(git rev-parse --short HEAD)${NC}"
else
    echo -e "${RED}  Deploy finished with ${ERRORS} failure(s) — review above${NC}"
fi
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# A non-zero exit when anything failed. The old script always exited 0, so a caller -- a CI step,
# a wrapper, the operator's ``&& echo done`` -- could not tell a clean deploy from one that came
# back with a dead frontend.
exit $(( ERRORS > 0 ? 1 : 0 ))
