# Aether Clinician

A clinician-facing diagnostic & management **decision-support system (CDSS)** for primary
care in India. It transforms fragmented patient histories (prescriptions, lab PDFs, scanned
reports) into a structured longitudinal record, runs **deterministic, offline-capable
drug-safety checks**, and keeps an **immutable, hash-chained audit trail** of every clinical
action. The clinician is always the decision-maker — the system supports, never prescribes.

> **Demo build — decision-support only, not for real patient care.**

This repository implements **all four phases**: Phase 1 foundation (records, drug safety,
audit), Phase 2 multi-agent reasoning engine + Reasoning Theatre, Phase 3 guideline RAG with
cited management, and Phase 4 clinical validation / regulatory / pilot. See
[`docs/implementation-plan.md`](docs/implementation-plan.md) for the original scope.

---

## What's implemented (Phase 1)

| Feature | Where | Notes |
|---|---|---|
| **Auth** — signup / login / refresh-rotation / logout / me | `apps/api/app/services/auth_service.py` | bcrypt + JWT access tokens, opaque hashed refresh tokens, session tracking |
| **Patient management** — CRUD, search, soft-delete | `…/patient_service.py` | Consent-gated creation (DPDP Act); search by name/phone; per-account isolation |
| **Document ingestion** — upload, validation, dedup | `…/document_service.py` | Magic-byte type sniffing, 20 MB cap, SHA-256 content-addressed storage + dedup |
| **Extraction** — Claude vision + OCR/text fallback | `…/services/extraction/` | Per-field confidence scoring; Tesseract + deterministic parser when no API key |
| **Patient graph** — meds / labs / conditions / allergies | `…/graph_service.py` | Brand→generic→reference-id normalization; dedup; **eGFR** (CKD-EPI 2021) derived markers |
| **Drug safety** — deterministic, offline | `app/core/safety.py` | Allergy (direct + cross-class), interactions, contraindications, renal dosing → **hard blocks** |
| **Immutable audit log** — SHA-256 hash chain | `…/audit_service.py`, `app/core/audit_hash.py` | Append-only; tamper-evident; per-patient + global chain verification |
| **Schema + migrations** | `data/migrations/` | Alembic; `updated_at` + audit-immutability triggers on PostgreSQL |
| **Frontend** | `apps/web/` | Next.js 14: auth, patient list/detail, upload + extraction review, drug-safety, audit |

## What's implemented (Phases 2–4)

| Feature | Where | Notes |
|---|---|---|
| **8-agent reasoning engine** | `apps/api/app/agents/` | Triage, parallel specialist hypothesis panel, can't-miss sentinel, devil's-advocate, investigation strategist, guideline-RAG, **verifier gatekeeper**, synthesis — a LangGraph-style `StateGraph` with a deterministic offline fallback |
| **Reasoning Theatre** | `apps/web/.../reasoning/`, SSE | Live agent lanes, hypotheses, pulsing can't-miss flags, non-collapsible dissent, verifier verdict streamed over Server-Sent Events |
| **Anti-automation-bias UX** | `SuggestionCard.tsx` | Evidence rendered **before** conclusions, mandatory devil's-advocate, active-engagement gates on flag-for-review, qualitative probability bands (never %) |
| **Immutable clinical suggestions** | `clinical_suggestion.py` | Append-only; Postgres UPDATE/DELETE trigger; clinician decisions captured separately; hard-block override needs documented reasoning |
| **Guideline RAG + cited management** | `guideline_service.py`, `app/agents/guideline_rag.py` | ICMR STW + WHO/NICE corpus, section-aware ingestion (Qdrant optional, lexical fallback), 0.75 grounding threshold, **citation-faithfulness** scoring (target ≥95%) |
| **Clinical validation harness** | `validation_service.py`, `data/validation/` | Gold vignettes through the full pipeline → top-1/top-3 accuracy, can't-miss recall, hard-block correctness, tier distribution; immutable `ValidationRun` records |
| **CDSCO SaMD dossier** | `regulatory_service.py` | Auto-generated (JSON + Markdown) from live metadata: risk controls, recomputed audit-chain integrity, latest validation metrics, DPDP governance |
| **Metrics dashboard + safety reporting + pilot** | `apps/web/.../metrics/`, `metrics_service.py` | Performance metrics, validation runner, append-only safety-report register, monitored-pilot status |

### Reasoning safety properties (enforced & tested)
- **The Verifier cannot be bypassed** — it sits on every path to synthesis.
- **Conservative output wins** on disagreement (band downgrade + tier escalation).
- **Can't-miss diagnoses are append-only** and force flag-for-review.
- **Offline → explicit degraded mode**, never silent confident output.

### Safety architecture highlights
- **Hard blocks cannot be overridden.** Allergy conflicts (including same-drug-class cross
  matches), absolute contraindications, `contraindicated`-severity interactions, and
  renal-threshold `contraindicated` actions all produce non-dismissible hard blocks.
- **Drug-safety checks are 100% deterministic** — no LLM, no network. The engine
  (`app/core/safety.py`) is a pure module so the exact same logic can be ported to the
  offline TypeScript client (Phase 1 roadmap item P1-10d).
- **The audit log is append-only and tamper-evident.** Each row is
  `sha256(prev_hash ‖ canonical_payload)`; any retroactive edit breaks the chain. PostgreSQL
  triggers additionally reject `UPDATE`/`DELETE` at the database level.
- **Drug names are never matched by string equality** — always resolved through the
  `DrugVocabulary` (exact, then fuzzy) so "Crocin" → Paracetamol, "Glycomet" → Metformin.

---

## Architecture

```
apps/web (Next.js 14)  ──REST──►  apps/api (FastAPI, async)
                                      │
                 ┌────────────────────┼─────────────────────┐
                 ▼                    ▼                     ▼
           PostgreSQL 16          (Redis 7)        services/extraction
        patient graph +         cache/sessions      Claude vision +
        immutable audit         (Phase 2+)          Tesseract/text fallback
```

Backend follows a layered architecture: **routers → services → models** (all async).
Pure, safety-critical logic (drug safety, eGFR, audit hashing, password/JWT) lives in
`app/core/` and is unit-tested without a database.

---

## Quick start

### Option A — Docker (full stack)
```bash
cp .env.example .env                 # set APP_SECRET_KEY; ANTHROPIC_API_KEY optional
docker compose up --build            # api :8000, web :3000, postgres, redis, qdrant, minio
```
The API container runs migrations + seeds the drug vocabulary on startup.

### Option B — Local dev
```bash
./scripts/dev-setup.sh               # infra + venv + deps + migrate + seed + web deps

# API
cd apps/api && ../../.venv/bin/uvicorn app.main:app --reload

# Web (separate terminal)
cd apps/web && npm run dev           # http://localhost:3000
```

Open <http://localhost:3000>, sign up, create a patient (with consent), upload a
prescription/lab document, confirm the extraction, then run a drug-safety check.

> **No Postgres handy?** The stack runs on SQLite for development/tests by setting
> `DATABASE_URL="sqlite+aiosqlite:///./aether.db"`. The portable column types in
> `app/db/types.py` emit native PostgreSQL types (UUID/JSONB/INET) on PG and fall back on
> SQLite. The immutability/`updated_at` **triggers are PostgreSQL-only**; on SQLite the
> append-only guarantee is enforced at the application + hash-chain layers.

---

## Testing

```bash
# Backend — 1,489 tests
cd apps/api
../../.venv/bin/python -m pytest
../../.venv/bin/python -m pytest --cov=app       # 100% statement coverage
../../.venv/bin/ruff check app tests             # lint
../../.venv/bin/mypy app                         # strict type check

# Frontend — 355 tests across 28 files, 100% line/branch coverage
cd apps/web && npm run test -- --run --coverage
```

Test coverage includes a parameterized **drug-safety hard-block matrix**, the **eGFR**
formula, the **audit hash-chain** (including tamper detection), auth flows, patient CRUD +
isolation, and the full **upload → extract → approve → graph-merge** path with eGFR
computation and brand-name normalization.

### Testing against PostgreSQL

The default suite runs on in-memory SQLite, which is fast but **does not enforce several
constraints PostgreSQL does** — `VARCHAR(n)` lengths and `NUMERIC(p, s)` range are both
ignored, and the immutability/`updated_at` triggers do not exist at all. Every deployment
runs on PostgreSQL, so a green suite on its own says nothing about those paths. This is not
hypothetical: two overflow bugs reached production through exactly this gap, where a lab
value or drug name too large for its column passed every test and then failed at `flush` —
which does not fail the one bad field, it 500s the clinician's whole document approval.

Two things close it:

- `apps/api/tests/column_fit.py` reapplies the column limits from the SQLite side, so an
  ordinary `pytest` run catches a regression.
- `apps/api/tests/test_postgres_column_bounds.py` (marked `postgres`) proves it end to end
  against a real server. It **skips itself when none is reachable**, so it is safe to leave
  in the default run:

  ```bash
  docker compose up -d postgres
  cd apps/api && ../../.venv/bin/python -m pytest -m postgres
  # point it elsewhere with TEST_POSTGRES_URL; it builds and drops its own schema,
  # so it never touches existing data
  ```

  Deselect with `-m 'not postgres'`.

**When adding a column-typed constraint** (a narrower `String(n)`, a `Numeric`, a
`CHECK ... IN (...)`) to anything the extraction pipeline writes, assume the SQLite suite
will not catch a violation and add the PostgreSQL case.

---

## Key commands

```bash
# Migrations (from repo root, DATABASE_URL set)
.venv/bin/python -m alembic -c data/migrations/alembic.ini upgrade head
# Seed reference drug data
./scripts/seed-db.sh
# Frontend build / typecheck
cd apps/web && npm run build
```

---

## Repository layout

```
apps/api/            FastAPI backend (routers, services, models, core logic, tests)
apps/web/            Next.js 14 frontend
data/migrations/     Alembic migrations (single source of truth = ORM metadata)
data/drugs/          Curated drug vocabulary, interactions, contraindications (JSON seed)
packages/            Shared config / types / ui (consolidation ongoing)
services/            Phase 2 (reasoning) and Phase 3 (rag) — scaffolded
docs/                Full architecture, system, DB, API, and implementation specs
```

See [`CLAUDE.md`](CLAUDE.md) for the non-negotiable clinical safety rules and the full
project context.

---

## Status & scope

Phase 1 is feature-complete against [`docs/implementation-plan.md`](docs/implementation-plan.md)
§2 for the core record/safety/audit system. Deferred to later Phase-1 polish or Phase 2:
full PWA service worker + IndexedDB offline parity (P1-10a–c), the async Redis-Stream
extraction worker with SSE progress (P1-05c), and medication-timeline / lab-trend charts
(P1-07). These are noted inline and do not affect the safety-critical guarantees above.
