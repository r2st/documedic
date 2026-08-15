#!/usr/bin/env bash
# First-time local development setup for Aether Clinician.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Starting infrastructure (Postgres, Redis, Qdrant, MinIO)…"
docker compose up -d postgres redis qdrant minio

echo "==> Creating Python venv and installing backend deps…"
python3.12 -m venv .venv
.venv/bin/pip install -q --upgrade pip
# Dev setup wants the test/lint tooling too; the Dockerfile deliberately installs only
# requirements.txt so none of it ships in the production image.
.venv/bin/pip install -q -r apps/api/requirements-dev.txt

echo "==> Applying database migrations…"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://aether:aether@localhost:5432/aether_clinician}"
.venv/bin/python -m alembic -c data/migrations/alembic.ini upgrade head

echo "==> Seeding drug vocabulary, interactions, contraindications…"
( cd apps/api && PYTHONPATH=. ../../.venv/bin/python -m app.db.seed )

echo "==> Installing frontend deps…"
( cd apps/web && npm install --no-audit --no-fund )

echo "Done. Start the API:  cd apps/api && ../../.venv/bin/uvicorn app.main:app --reload"
echo "      Start the web:  cd apps/web && npm run dev"
