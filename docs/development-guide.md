# Aether Clinician -- Developer Setup and Conventions Guide

**Product:** Aether Clinician -- Clinician-Facing Diagnostic & Management Decision-Support System  
**Version:** 1.0  
**Last Updated:** 2026-06-27  
**Status:** Living document  
**Audience:** All contributors (engineering, design, QA, clinical safety reviewers)

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Initial Setup](#2-initial-setup)
3. [Environment Variables](#3-environment-variables)
4. [Running Locally](#4-running-locally)
5. [Testing Approach](#5-testing-approach)
6. [Code Style and Linting](#6-code-style-and-linting)
7. [Git Workflow and Commit Conventions](#7-git-workflow-and-commit-conventions)
8. [Architecture Patterns for Contributors](#8-architecture-patterns-for-contributors)
9. [Deployment Process](#9-deployment-process)
10. [Common Tasks](#10-common-tasks)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Prerequisites

### 1.1 Required Software

| Software             | Minimum Version | Installation Notes                                                                 |
|----------------------|-----------------|------------------------------------------------------------------------------------|
| Python               | 3.12+           | Use `pyenv` to manage versions. The project uses 3.12 features (type parameter syntax, `override` decorator). |
| Node.js              | 20 LTS+         | Use `nvm` or `fnm` to manage versions. The Next.js 14 App Router requires Node 18.17+, but 20 LTS is the project baseline. |
| pnpm                 | 9+              | Install globally: `npm install -g pnpm@latest`. Required for the Turborepo workspace. Do not use `npm` or `yarn` for package installation. |
| Docker               | 24+             | Docker Desktop (macOS/Windows) or Docker Engine (Linux). Required for infrastructure services. |
| Docker Compose       | 2.20+           | Included with Docker Desktop. Verify with `docker compose version` (note: `docker-compose` with a hyphen is the legacy v1 binary -- do not use it). |
| PostgreSQL Client    | 16+             | `psql` CLI for database inspection. Install via Homebrew (`brew install postgresql@16`) or your system package manager. The server runs in Docker -- you only need the client tools locally. |
| Tesseract OCR        | 5.x             | macOS: `brew install tesseract`. Ubuntu: `apt install tesseract-ocr tesseract-ocr-hin`. Requires English and Hindi language packs (`eng` + `hin`). |
| Git                  | 2.40+           | Any recent version. The project uses conventional commits enforced by a pre-commit hook. |
| Ruff                 | 0.4+            | Python linter and formatter. Install globally (`pip install ruff`) or let the virtual environment handle it (it is in `pyproject.toml` dev dependencies). |

### 1.2 Required Accounts

| Account              | Purpose                                                      | How to Obtain                                      |
|----------------------|--------------------------------------------------------------|----------------------------------------------------|
| Anthropic API key    | Powers all LLM features: agent reasoning, document extraction, guideline RAG. | Sign up at [console.anthropic.com](https://console.anthropic.com). You need a key with access to `claude-sonnet-4-20250514`. |

No other external accounts are required for local development. PostgreSQL, Redis, Qdrant, and MinIO all run locally via Docker Compose.

### 1.3 Recommended VS Code Extensions

The repository includes a `.vscode/extensions.json` with workspace recommendations. Key extensions:

| Extension                        | Purpose                                                       |
|----------------------------------|---------------------------------------------------------------|
| `ms-python.python`               | Python language support, debugger, virtual environment detection |
| `charliermarsh.ruff`             | Ruff linter/formatter integration (replaces Black, isort, Flake8) |
| `ms-python.mypy-type-checker`    | mypy integration for real-time type error feedback              |
| `dbaeumer.vscode-eslint`         | ESLint integration for TypeScript/React                        |
| `esbenp.prettier-vscode`         | Prettier formatter for TypeScript, JSON, CSS, Markdown         |
| `bradlc.vscode-tailwindcss`      | Tailwind CSS IntelliSense (class autocomplete, hover previews) |
| `ms-playwright.playwright`       | Playwright test runner integration                              |
| `ms-azuretools.vscode-docker`    | Dockerfile syntax, Docker Compose support, container management |
| `mtxr.sqltools`                  | SQL editor and database explorer for PostgreSQL                 |
| `mtxr.sqltools-driver-pg`        | PostgreSQL driver for SQLTools                                  |

### 1.4 Hardware Recommendations

Aether Clinician runs the full stack locally (5 Docker containers + 2 application servers + build tooling). Recommended minimums:

| Resource | Minimum       | Recommended    | Notes                                                     |
|----------|---------------|----------------|-----------------------------------------------------------|
| RAM      | 8 GB          | 16 GB          | Docker services alone consume ~3 GB. LangGraph agent orchestration is memory-intensive when running multiple concurrent reasoning sessions. |
| CPU      | 4 cores       | 8 cores        | Turborepo parallelizes builds and tests. Tesseract OCR is CPU-bound. |
| Disk     | 10 GB free    | 20 GB free     | Docker images, Qdrant vector data, uploaded test documents, and node_modules. |
| Network  | Broadband     | Broadband       | Required for Anthropic Claude API calls. Expect ~2-5 MB per full reasoning session (8-agent pipeline). Offline mode covers core safety checks but not LLM features. |

---

## 2. Initial Setup

Follow these steps in order. Each step depends on the previous one.

### Step 1: Clone the Repository

```bash
git clone git@github.com:<org>/documedic.git
cd documedic
```

### Step 2: Set Up the Python Virtual Environment

The backend and all Python services share a single `pyproject.toml` at the project root.

```bash
# Create virtual environment
python3.12 -m venv .venv

# Activate it
source .venv/bin/activate    # macOS/Linux
# .venv\Scripts\activate     # Windows (PowerShell)

# Install all Python dependencies (including dev dependencies)
pip install -e ".[dev]"
```

This installs FastAPI, LangGraph, SQLAlchemy, Alembic, Ruff, mypy, pytest, and all other Python dependencies defined in `pyproject.toml`.

### Step 3: Install Node.js Dependencies

```bash
pnpm install
```

This installs dependencies for all workspaces: `apps/web`, `packages/ui`, `packages/shared-types`, and `packages/config`. Turborepo configuration is in `turbo.json` at the project root.

### Step 4: Configure Environment Variables

```bash
# Copy the example environment file
cp .env.example .env

# Copy the frontend-specific environment file
cp apps/web/.env.example apps/web/.env.local
```

Edit `.env` and fill in the required values. At minimum, you need:

- `ANTHROPIC_API_KEY` -- your Anthropic API key
- `APP_SECRET_KEY` -- generate with `openssl rand -hex 32`

All other values have sensible defaults for local development. See [Section 3: Environment Variables](#3-environment-variables) for the complete reference.

### Step 5: Start Infrastructure Services

```bash
docker compose up -d
```

This starts:
- **PostgreSQL 16** on port `5432`
- **Redis 7** on port `6379`
- **Qdrant** on port `6333` (HTTP) and `6334` (gRPC)
- **MinIO** on port `9000` (S3 API) and `9001` (console)

Verify all containers are healthy:

```bash
docker compose ps
```

All services should show status `running (healthy)`.

### Step 6: Run Database Migrations

```bash
# With virtual environment activated
alembic upgrade head
```

This creates all tables, triggers (including the immutability trigger on `clinical_suggestions`), indexes, and enum types defined in the schema.

### Step 7: Seed Data

```bash
# Seed the drug vocabulary (Indian brand names -> INN generics)
python -m scripts.seed_drugs

# Seed the demo guideline corpus into Qdrant
python -m scripts.seed_guidelines

# (Optional) Create a demo clinician account
python -m scripts.seed_demo_user
```

The drug vocabulary seed loads data from `data/drugs/` -- Indian brand-name-to-generic mappings required for allergy cross-checking and drug interaction detection. The guideline seed loads curated clinical guidelines from `data/guidelines/` into the Qdrant vector store for RAG retrieval.

### Step 8: Verify Setup

```bash
python -m scripts.healthcheck
```

The health check script verifies:
- PostgreSQL connection and migration status
- Redis connection
- Qdrant connection and collection existence
- MinIO/S3 bucket accessibility
- Tesseract OCR binary availability
- Anthropic API key validity (makes a minimal API call)
- Drug vocabulary table population
- Guideline corpus vector count

Expected output on success:

```
[OK] PostgreSQL: connected, migrations up to date
[OK] Redis: connected
[OK] Qdrant: connected, collection 'guidelines' found (N vectors)
[OK] MinIO: bucket 'aether-documents' accessible
[OK] Tesseract: v5.x.x, languages: eng, hin
[OK] Anthropic API: key valid, model claude-sonnet-4-20250514 accessible
[OK] Drug vocabulary: N entries loaded
[OK] All checks passed. Ready to develop.
```

If the Anthropic API check fails, all other features still work -- LLM-dependent features will be unavailable but core safety checks, CRUD operations, and UI development proceed normally.

---

## 3. Environment Variables

All environment variables are defined in `.env` at the project root (backend/services) and `apps/web/.env.local` (frontend). The `.env.example` file contains all variables with documentation and safe defaults.

### 3.1 Application

| Variable            | Required | Default        | Purpose                                                          | Example                             |
|---------------------|----------|----------------|------------------------------------------------------------------|--------------------------------------|
| `APP_ENV`           | Yes      | `development`  | Runtime environment. Controls logging level, debug mode, CORS.   | `development`, `staging`, `production` |
| `APP_SECRET_KEY`    | Yes      | --             | JWT signing secret. Generate with `openssl rand -hex 32`.        | `a1b2c3d4e5...` (64-char hex)        |
| `APP_DEBUG`         | No       | `true`         | Enables debug mode (detailed error responses, Swagger UI). Must be `false` in production. | `true` or `false`                    |
| `DEMO_MODE`         | No       | `true`         | Enables demo mode: removes credential gate, shows persistent demo banner. Safety checks still apply. | `true` or `false`                    |

### 3.2 Database (PostgreSQL)

| Variable                 | Required | Default                                                      | Purpose                                      | Example                                                         |
|--------------------------|----------|--------------------------------------------------------------|----------------------------------------------|-----------------------------------------------------------------|
| `DATABASE_URL`           | Yes      | `postgresql+asyncpg://aether:aether@localhost:5432/aether_clinician` | Async database connection string. Must use the `asyncpg` driver. | `postgresql+asyncpg://user:pass@host:5432/dbname`               |
| `DATABASE_POOL_SIZE`     | No       | `20`                                                         | SQLAlchemy connection pool size.              | `20`                                                            |
| `DATABASE_MAX_OVERFLOW`  | No       | `10`                                                         | Maximum overflow connections beyond pool size.| `10`                                                            |

### 3.3 Redis

| Variable      | Required | Default                    | Purpose                                          | Example                         |
|---------------|----------|----------------------------|--------------------------------------------------|---------------------------------|
| `REDIS_URL`   | Yes      | `redis://localhost:6379/0` | Redis connection URL. Used for session cache, rate limiting, pub/sub for SSE streaming, and agent result caching. | `redis://localhost:6379/0`      |

### 3.4 Qdrant (Vector Store)

| Variable              | Required | Default                     | Purpose                                              | Example                        |
|-----------------------|----------|-----------------------------|------------------------------------------------------|---------------------------------|
| `QDRANT_URL`          | Yes      | `http://localhost:6333`     | Qdrant HTTP API endpoint.                            | `http://localhost:6333`         |
| `QDRANT_COLLECTION`   | No       | `guidelines`                | Collection name for the clinical guideline corpus.   | `guidelines`                    |
| `QDRANT_API_KEY`      | No       | --                          | API key for Qdrant authentication. Not needed for local development. | `your-qdrant-api-key`           |

### 3.5 Anthropic (LLM)

| Variable                | Required | Default                   | Purpose                                                           | Example                        |
|-------------------------|----------|---------------------------|-------------------------------------------------------------------|---------------------------------|
| `ANTHROPIC_API_KEY`     | Yes*     | --                        | API key for Claude. *Required for LLM features; system functions without it for CRUD and safety checks. | `sk-ant-...`                    |
| `ANTHROPIC_MODEL`       | No       | `claude-sonnet-4-20250514`      | Default model for reasoning agents.                               | `claude-sonnet-4-20250514`            |
| `ANTHROPIC_EXTRACTION_MODEL` | No  | `claude-sonnet-4-20250514`      | Model used for document extraction (vision).                      | `claude-sonnet-4-20250514`            |
| `ANTHROPIC_MAX_TOKENS`  | No       | `4096`                    | Default max tokens per agent invocation.                          | `4096`                          |

### 3.6 Authentication / JWT

| Variable                  | Required | Default     | Purpose                                                        | Example          |
|---------------------------|----------|-------------|----------------------------------------------------------------|------------------|
| `JWT_ACCESS_TOKEN_EXPIRE` | No       | `15`        | Access token lifetime in minutes.                              | `15`             |
| `JWT_REFRESH_TOKEN_EXPIRE`| No       | `10080`     | Refresh token lifetime in minutes (default: 7 days).           | `10080`          |
| `BCRYPT_COST_FACTOR`      | No       | `12`        | bcrypt hashing cost factor.                                    | `12`             |

### 3.7 File Storage (S3-Compatible)

| Variable             | Required | Default                      | Purpose                                          | Example                         |
|----------------------|----------|------------------------------|--------------------------------------------------|---------------------------------|
| `S3_ENDPOINT_URL`    | Yes      | `http://localhost:9000`      | S3-compatible endpoint. MinIO for dev, S3 for prod. | `http://localhost:9000`          |
| `S3_ACCESS_KEY`      | Yes      | `minioadmin`                 | S3 access key.                                   | `minioadmin`                     |
| `S3_SECRET_KEY`      | Yes      | `minioadmin`                 | S3 secret key.                                   | `minioadmin`                     |
| `S3_BUCKET_NAME`     | No       | `aether-documents`           | Bucket for uploaded patient documents.           | `aether-documents`               |
| `S3_REGION`          | No       | `ap-south-1`                 | AWS region. Must be India for production (DPDP compliance). | `ap-south-1`                     |

### 3.8 OCR

| Variable         | Required | Default                | Purpose                                 | Example                  |
|------------------|----------|------------------------|-----------------------------------------|--------------------------|
| `TESSERACT_CMD`  | No       | `/usr/bin/tesseract`   | Path to the Tesseract binary. macOS Homebrew installs to `/opt/homebrew/bin/tesseract`. | `/opt/homebrew/bin/tesseract` |

### 3.9 Server

| Variable       | Required | Default     | Purpose                              | Example     |
|----------------|----------|-------------|--------------------------------------|-------------|
| `API_HOST`     | No       | `0.0.0.0`  | FastAPI bind address.                | `0.0.0.0`  |
| `API_PORT`     | No       | `8000`     | FastAPI bind port.                   | `8000`      |
| `WEB_PORT`     | No       | `3000`     | Next.js dev server port.             | `3000`      |
| `LOG_LEVEL`    | No       | `INFO`     | Logging level for structlog.         | `DEBUG`, `INFO`, `WARNING` |
| `CORS_ORIGINS` | No       | `http://localhost:3000` | Comma-separated allowed CORS origins. | `http://localhost:3000,http://localhost:3001` |

### 3.10 Feature Flags

| Variable                         | Required | Default | Purpose                                                     | Example          |
|----------------------------------|----------|---------|-------------------------------------------------------------|------------------|
| `FF_REASONING_THEATRE`           | No       | `true`  | Enable the Reasoning Theatre UI (agent deliberation transparency). | `true` or `false` |
| `FF_GUIDELINE_RAG`               | No       | `true`  | Enable the Guideline-RAG Agent. When disabled, reasoning runs without guideline retrieval. | `true` or `false` |
| `FF_OFFLINE_MODE`                | No       | `false` | Force offline mode for testing. LLM features disabled, deterministic safety checks only. | `true` or `false` |
| `FF_DEVILS_ADVOCATE`             | No       | `true`  | Enable the Devil's-Advocate Agent. Cannot be disabled in production. | `true` or `false` |

### 3.11 Frontend (`apps/web/.env.local`)

| Variable                   | Required | Default                          | Purpose                                    | Example                              |
|----------------------------|----------|----------------------------------|--------------------------------------------|---------------------------------------|
| `NEXT_PUBLIC_API_URL`      | Yes      | `http://localhost:8000`          | Backend API base URL (used by API client). | `http://localhost:8000`               |
| `NEXT_PUBLIC_WS_URL`       | Yes      | `ws://localhost:8000/ws`         | WebSocket URL for reasoning theatre streaming. | `ws://localhost:8000/ws`              |
| `NEXT_PUBLIC_DEMO_MODE`    | No       | `true`                           | Show demo banner in the UI.                | `true` or `false`                     |

---

## 4. Running Locally

### 4.1 Start Everything with Turborepo

The simplest way to start the full development stack:

```bash
# 1. Start infrastructure (if not already running)
docker compose up -d

# 2. Start all application services
turbo dev
```

`turbo dev` starts:
- **FastAPI backend** (`apps/api`) on `http://localhost:8000`
- **Next.js frontend** (`apps/web`) on `http://localhost:3000`
- **Reasoning engine** (`services/reasoning`) in watch mode

### 4.2 Start Individual Services

When you are working on a single layer, start only what you need:

```bash
# Backend API server (with hot reload)
cd apps/api
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Frontend dev server
cd apps/web
pnpm dev

# Reasoning engine (standalone, for testing agent pipelines)
cd services/reasoning
python -m reasoning.graph --dry-run    # Validate graph structure
python -m reasoning.server             # Start as a service
```

### 4.3 Docker Compose Full Stack

To run everything including application services in Docker (mirrors staging/production):

```bash
docker compose --profile full up -d
```

This starts infrastructure services plus containerized versions of the API, web, and reasoning services. Useful for testing production-like deployments but slower iteration than the native dev servers.

### 4.4 Hot Reload Behavior

| Service           | Hot Reload Mechanism            | What Triggers a Reload                                  |
|-------------------|---------------------------------|---------------------------------------------------------|
| FastAPI backend   | Uvicorn `--reload`              | Any `.py` file change in `apps/api/`                    |
| Next.js frontend  | Next.js Fast Refresh            | Any `.tsx`, `.ts`, `.css` change in `apps/web/src/`     |
| Reasoning engine  | Manual restart                  | Changes to agent prompts or graph definitions require a restart. `turbo dev` handles this via file watchers. |
| Shared packages   | Turborepo dependency tracking   | Changes in `packages/shared-types/` or `packages/ui/` trigger dependent workspace rebuilds. |

### 4.5 Accessing Services

| Service           | URL                              | Notes                                          |
|-------------------|----------------------------------|-------------------------------------------------|
| Web UI            | `http://localhost:3000`          | Next.js frontend. Main development interface.   |
| API (REST)        | `http://localhost:8000`          | FastAPI backend. All REST endpoints.             |
| API docs (Swagger)| `http://localhost:8000/docs`     | Interactive API documentation. Available when `APP_DEBUG=true`. |
| API docs (ReDoc)  | `http://localhost:8000/redoc`    | Alternative API documentation view.              |
| WebSocket (SSE)   | `ws://localhost:8000/ws`         | Reasoning Theatre streaming endpoint.            |
| PostgreSQL        | `localhost:5432`                 | Connect with `psql -h localhost -U aether -d aether_clinician`. |
| Redis             | `localhost:6379`                 | Connect with `redis-cli`.                        |
| Qdrant dashboard  | `http://localhost:6333/dashboard`| Qdrant web UI for inspecting vector collections. |
| MinIO console     | `http://localhost:9001`          | S3 storage web UI. Default credentials: `minioadmin`/`minioadmin`. |

### 4.6 Useful Dev Scripts

```bash
# Format all Python code
ruff format .

# Lint all Python code (with auto-fix)
ruff check . --fix

# Type-check Python
mypy apps/api/app/ services/reasoning/ services/extraction/ services/rag/

# Lint all TypeScript/React code
turbo lint

# Type-check TypeScript
turbo typecheck

# Generate a new Alembic migration
alembic revision --autogenerate -m "description of change"

# Reset the local database (drop + recreate + migrate + seed)
python -m scripts.reset_db

# Validate the LangGraph agent graph structure (no LLM calls)
python -m reasoning.graph --dry-run
```

---

## 5. Testing Approach

Aether Clinician is a clinical safety system. Testing is not optional. Every PR must maintain or increase test coverage. Drug-safety logic requires 100% branch coverage.

### 5.1 Unit Tests (Python) -- pytest

**Location:** Tests live alongside the code they test in a `tests/` subdirectory, or in `apps/api/tests/`, `services/reasoning/tests/`, etc.

**Run:**

```bash
# All Python tests
pytest

# With coverage report
pytest --cov=app --cov=services --cov-report=term-missing

# Single file
pytest apps/api/tests/test_patient_service.py

# Single test
pytest apps/api/tests/test_patient_service.py::test_create_patient_with_allergy

# Verbose output
pytest -v

# Stop on first failure
pytest -x
```

**Test structure:**

```python
# apps/api/tests/test_patient_service.py

import pytest
from unittest.mock import AsyncMock, patch
from app.services.patient import PatientService
from app.schemas.patient import PatientCreate

@pytest.fixture
def patient_service(test_db_session):
    """PatientService with a test database session."""
    return PatientService(session=test_db_session)

@pytest.fixture
def sample_patient_data():
    return PatientCreate(
        name="Test Patient",
        date_of_birth="1985-03-15",
        gender="male",
    )

async def test_create_patient(patient_service, sample_patient_data):
    patient = await patient_service.create(sample_patient_data)
    assert patient.id is not None
    assert patient.name == "Test Patient"

async def test_create_patient_with_allergy(patient_service, sample_patient_data):
    sample_patient_data.allergies = [{"substance": "Paracetamol", "severity": "severe"}]
    patient = await patient_service.create(sample_patient_data)
    assert len(patient.allergies) == 1
    assert patient.allergies[0].substance == "Paracetamol"
```

**Mocking the Claude API:**

All tests that would call the Anthropic API must mock it. Never make real LLM calls in unit tests.

```python
# Fixture for mocking Claude API responses
@pytest.fixture
def mock_anthropic():
    with patch("app.services.extraction.anthropic_client") as mock:
        mock.messages.create = AsyncMock(return_value=MockMessage(
            content=[MockTextBlock(text='{"medication": "Paracetamol", "dosage": "500mg"}')]
        ))
        yield mock

async def test_extract_prescription(mock_anthropic, extraction_service):
    result = await extraction_service.extract_prescription(test_image_bytes)
    assert result.medication == "Paracetamol"
    mock_anthropic.messages.create.assert_called_once()
```

**Key fixtures** (defined in `conftest.py`):

| Fixture              | Purpose                                                              |
|----------------------|----------------------------------------------------------------------|
| `test_db_session`    | Async SQLAlchemy session connected to the test database. Rolls back after each test. |
| `test_client`        | FastAPI `TestClient` (async) configured with the test database.       |
| `mock_anthropic`     | Mocked Anthropic client that returns configurable responses.          |
| `sample_patient`     | Pre-created patient record with demographics, allergies, and medications. |
| `sample_encounter`   | Pre-created encounter linked to `sample_patient`.                     |

### 5.2 Unit Tests (TypeScript) -- Vitest + React Testing Library

**Location:** Test files live next to the component they test with a `.test.tsx` or `.test.ts` suffix.

**Run:**

```bash
# All frontend tests
cd apps/web
pnpm test

# Watch mode
pnpm test -- --watch

# Single file
pnpm test -- src/components/PatientCard.test.tsx

# With coverage
pnpm test -- --coverage
```

**Component testing patterns:**

```tsx
// apps/web/src/components/PatientCard.test.tsx

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PatientCard } from "./PatientCard";

describe("PatientCard", () => {
  it("displays patient name and age", () => {
    render(
      <PatientCard
        patient={{ id: "1", name: "Rajesh Kumar", age: 45, gender: "male" }}
      />
    );
    expect(screen.getByText("Rajesh Kumar")).toBeInTheDocument();
    expect(screen.getByText("45 years")).toBeInTheDocument();
  });

  it("shows allergy badge when allergies exist", () => {
    render(
      <PatientCard
        patient={{
          id: "1",
          name: "Rajesh Kumar",
          age: 45,
          gender: "male",
          allergies: [{ substance: "Paracetamol", severity: "severe" }],
        }}
      />
    );
    expect(screen.getByText("Paracetamol")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});
```

**Testing hooks and API calls:**

```tsx
// Mock TanStack Query for API-dependent components
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const createTestQueryClient = () =>
  new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

function renderWithQuery(ui: React.ReactElement) {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      {ui}
    </QueryClientProvider>
  );
}
```

### 5.3 Integration Tests

**API endpoint tests** run against a real test database (PostgreSQL in Docker) with the full FastAPI application:

```python
# apps/api/tests/integration/test_patient_endpoints.py

async def test_create_patient_endpoint(test_client, auth_headers):
    response = await test_client.post(
        "/api/v1/patients",
        json={"name": "Test Patient", "date_of_birth": "1985-03-15", "gender": "male"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["name"] == "Test Patient"
    assert "id" in data
```

**Agent pipeline integration tests** validate that agents compose correctly through the LangGraph graph:

```python
# services/reasoning/tests/integration/test_reasoning_pipeline.py

async def test_full_pipeline_routes_through_verifier(mock_anthropic, sample_case_state):
    """Every clinical output must pass through the Verifier Agent."""
    result = await run_reasoning_pipeline(sample_case_state)
    assert result.verifier_passed is True
    assert result.autonomy_tier in ("informational", "suggestive", "flag_for_review")
```

Run integration tests:

```bash
# Python integration tests (requires Docker services running)
pytest tests/integration/ -m integration

# Full integration suite
pytest -m "integration" --tb=short
```

#### 5.3.1 What SQLite does not enforce (and why it has bitten us)

The default `pytest` run uses an in-memory SQLite database — fast, isolated per test, no
Docker required. It is the right default, but it is **weaker than the database we deploy on**,
and the difference is silent:

| Constraint | SQLite | PostgreSQL |
|---|---|---|
| `VARCHAR(n)` length | ignored — stores any length | `value too long for type character varying(n)` |
| `NUMERIC(p, s)` range | ignored — stores any magnitude | `numeric field overflow` |
| `CHECK ... IN (...)` | **enforced** | enforced |
| `UPDATE`/`DELETE` immutability triggers | absent | enforced |
| `updated_at` triggers | absent | enforced |

The first two rows are the dangerous ones, because they make a real bug look like a passing
test. Both have shipped: a lab value of `99999999999999999999999.5` read off a scan with a
smudged decimal point, and a 900-character run of OCR noise read as one drug name. Each was
stored happily in every test and rejected by PostgreSQL in production.

The blast radius is larger than the bad field. The rejection lands at `flush`, by which point
the whole approval is one transaction — so it does not drop the unreadable line, it returns a
500 and rolls back *every* entity that extracted correctly on the same document. The
clinician loses the whole chart, not the one value the scanner could not read.

**Writing tests for this class of bug.** Two layers, both required:

1. **`apps/api/tests/column_fit.py`** — `assert_fits_columns(*rows)` reapplies the column
   limits SQLite ignores, reading them off the mapped columns. Sweep the rows a merge writes
   with it, and the ordinary suite catches the regression:

   ```python
   from tests.column_fit import assert_fits_columns

   rows = (await db.execute(select(LabResult))).scalars().all()
   assert_fits_columns(*rows)
   ```

2. **`apps/api/tests/test_postgres_column_bounds.py`** — the end-to-end confirmation against a
   real server, marked `postgres`. The fixture creates its own PostgreSQL schema and drops it
   afterwards, so it never touches existing data, and it **skips when nothing is reachable**:

   ```bash
   docker compose up -d postgres
   cd apps/api && pytest -m postgres              # deselect with -m 'not postgres'
   TEST_POSTGRES_URL=postgresql+asyncpg://… pytest -m postgres
   ```

   Note that this module first asserts PostgreSQL *does* reject an over-long string and an
   out-of-range numeric, before testing that the merge avoids both. Without those two, the
   whole file could pass by connecting to something that enforces nothing.

**Rule of thumb:** if you add a `String(n)`, a `Numeric(p, s)`, or a `CHECK` to any column the
extraction pipeline writes, assume the SQLite suite will not catch a violation. Prefer fitting
the value at the merge boundary (see `_fitted`, `_to_decimal`, and `_enum` in
`app/services/graph_service.py`) over letting the database refuse it — one unreadable line
must never cost the clinician the rest of the document.

### 5.4 E2E Tests -- Playwright

End-to-end tests exercise the full stack from the browser through the API to the database.

**Location:** `apps/web/e2e/`

**Run:**

```bash
cd apps/web

# Run all E2E tests
pnpm exec playwright test

# Run with UI mode (interactive)
pnpm exec playwright test --ui

# Run a specific test file
pnpm exec playwright test e2e/patient-intake.spec.ts

# Generate test code by recording interactions
pnpm exec playwright codegen http://localhost:3000
```

**Key user flows to test:**

| Flow                           | What It Validates                                                              |
|--------------------------------|--------------------------------------------------------------------------------|
| Patient registration           | Create patient, add demographics, save.                                        |
| Document upload + extraction   | Upload a prescription image, verify extracted medications appear in the patient record. |
| Allergy conflict detection     | Add a known allergy, prescribe a conflicting drug, verify the hard block fires. |
| Reasoning Theatre              | Trigger a diagnostic session, verify all agent panels render, devil's-advocate dissent is visible, evidence appears before conclusions. |
| Intake questionnaire           | Start a consult, verify clarifying questions appear, submit answers, verify they feed into reasoning. |
| Demo mode banner               | When `DEMO_MODE=true`, verify the persistent banner is visible on every page.  |

### 5.5 Agent Testing Patterns

Testing LLM-powered agents requires special patterns because agent output is non-deterministic.

**Testing individual agents:**

```python
# services/reasoning/tests/test_devils_advocate.py

async def test_devils_advocate_produces_counter_argument(mock_anthropic):
    """Devil's-advocate agent must produce a non-empty counter-argument."""
    mock_anthropic.messages.create.return_value = MockMessage(
        content=[MockTextBlock(text='{"counter_argument": "Consider viral etiology...", "evidence": [...]}')]
    )

    agent = DevilsAdvocateAgent(client=mock_anthropic)
    result = await agent.run(sample_case_state_with_hypothesis)

    assert result.counter_argument is not None
    assert len(result.counter_argument) > 0
    assert len(result.evidence) > 0
```

**Mock `CaseState` for testing:**

```python
@pytest.fixture
def sample_case_state():
    """A CaseState with enough data to exercise most agents."""
    return CaseState(
        patient_id="test-uuid",
        demographics=Demographics(age=45, gender="male"),
        medications=[Medication(name="Metformin", dosage="500mg", frequency="BD")],
        conditions=[Condition(name="Type 2 Diabetes", status="active")],
        allergies=[Allergy(substance="Sulfonamides", severity="severe")],
        lab_results=[LabResult(test="HbA1c", value=8.5, unit="%", reference_range="4.0-5.6")],
        current_complaint="Persistent cough and fever for 5 days",
    )
```

**Snapshot testing for agent outputs:**

For regression detection, snapshot the structure (not content) of agent outputs:

```python
async def test_hypothesis_agent_output_structure(mock_anthropic, sample_case_state):
    result = await run_hypothesis_agent(sample_case_state)

    # Validate structure, not LLM-generated content
    assert isinstance(result.hypotheses, list)
    assert len(result.hypotheses) >= 1
    for hypothesis in result.hypotheses:
        assert "diagnosis" in hypothesis
        assert "evidence_for" in hypothesis
        assert "evidence_against" in hypothesis
        assert "confidence" in hypothesis
        assert 0.0 <= hypothesis["confidence"] <= 1.0
```

### 5.6 Drug Safety Testing

Drug safety checks are deterministic (no LLM dependency) and require 100% branch coverage. These tests use hard-coded fixtures with known drug-allergy and drug-interaction pairs.

```python
# apps/api/tests/test_drug_safety.py

class TestAllergyConflictDetection:
    """Allergy conflicts must be hard blocks. No exceptions."""

    def test_direct_allergy_match(self, drug_safety_service):
        """Crocin (brand) -> Paracetamol (generic) -> known allergy."""
        result = drug_safety_service.check_allergy_conflict(
            drug_name="Crocin",
            patient_allergies=[Allergy(substance="Paracetamol", severity="severe")],
        )
        assert result.is_blocked is True
        assert result.block_type == "hard"
        assert "Paracetamol" in result.reason

    def test_cross_brand_allergy_resolution(self, drug_safety_service):
        """Dolo-650 and Crocin both resolve to Paracetamol."""
        result = drug_safety_service.check_allergy_conflict(
            drug_name="Dolo-650",
            patient_allergies=[Allergy(substance="Crocin", severity="moderate")],
        )
        assert result.is_blocked is True  # Both resolve to Paracetamol via DrugVocabulary

    def test_contraindication_detection(self, drug_safety_service):
        """Metformin is contraindicated in severe renal impairment."""
        result = drug_safety_service.check_contraindications(
            drug_name="Metformin",
            patient_conditions=[Condition(name="Chronic Kidney Disease Stage 4")],
            patient_labs=[LabResult(test="eGFR", value=22, unit="mL/min")],
        )
        assert result.is_blocked is True
        assert result.block_type == "hard"

    def test_drug_interaction_warning(self, drug_safety_service):
        """Warfarin + NSAIDs should flag an interaction."""
        result = drug_safety_service.check_interactions(
            drug_name="Ibuprofen",
            current_medications=[Medication(name="Warfarin", dosage="5mg")],
        )
        assert result.has_interaction is True
        assert result.severity in ("major", "contraindicated")
```

**These tests must pass offline.** They validate deterministic logic against the local DrugVocabulary, not LLM calls.

### 5.7 Test Data

| Location                        | Contents                                                       |
|---------------------------------|----------------------------------------------------------------|
| `tests/fixtures/`              | Shared test fixtures (JSON patient records, sample documents). |
| `tests/factories/`             | Factory functions for generating test entities (patients, medications, encounters). |
| `tests/fixtures/documents/`    | Sample prescription images, lab report PDFs, and scanned documents for extraction tests. |
| `data/drugs/test_subset.csv`   | Minimal drug vocabulary for fast test runs.                    |

### 5.8 Running Tests -- Summary

```bash
# All tests (Python + TypeScript + E2E)
turbo test

# Python unit tests only
pytest

# Python with coverage
pytest --cov=app --cov=services --cov-report=html

# TypeScript tests only
cd apps/web && pnpm test

# E2E tests only
cd apps/web && pnpm exec playwright test

# Integration tests only (requires Docker services)
pytest -m integration

# Constraint tests against a real PostgreSQL (see 5.3.1) -- these skip when none is
# reachable, so they are safe to leave in the default run
docker compose up -d postgres && pytest -m postgres
pytest -m 'not postgres'                      # deselect them

# Drug safety tests only
pytest apps/api/tests/test_drug_safety.py -v

# Agent tests only
pytest services/reasoning/tests/ -v
```

### 5.9 Coverage Requirements

| Module                     | Minimum Coverage | Notes                                                    |
|----------------------------|------------------|----------------------------------------------------------|
| Drug safety (`drug_safety/`) | 100% branch    | Deterministic safety logic. No exceptions.               |
| API services (`services/`) | 80% line         | Business logic layer.                                    |
| API routers (`routers/`)   | 70% line         | Request handling and validation.                         |
| Agent definitions          | 80% line         | Agent logic structure (LLM calls are mocked).            |
| Frontend components        | 70% line         | React components.                                        |
| Shared packages            | 80% line         | Type definitions, utility functions.                     |

CI blocks merges when coverage drops below these thresholds.

---

## 6. Code Style and Linting

### 6.1 Python -- Ruff

Ruff is the single tool for Python linting and formatting. It replaces Black, isort, Flake8, and most other Python linting tools.

**Configuration** (in `pyproject.toml`):

```toml
[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = [
    "E",    # pycodestyle errors
    "W",    # pycodestyle warnings
    "F",    # pyflakes
    "I",    # isort (import sorting)
    "N",    # pep8-naming
    "UP",   # pyupgrade
    "B",    # flake8-bugbear
    "SIM",  # flake8-simplify
    "TCH",  # flake8-type-checking
    "RUF",  # ruff-specific rules
    "ASYNC",# flake8-async
    "S",    # flake8-bandit (security)
]
ignore = [
    "E501",  # line length (handled by formatter)
]

[tool.ruff.lint.isort]
known-first-party = ["app", "services", "packages"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
```

**Commands:**

```bash
# Check for lint errors
ruff check .

# Auto-fix safe lint errors
ruff check . --fix

# Format code
ruff format .

# Check formatting without modifying files
ruff format . --check
```

### 6.2 Python -- mypy (Strict Mode)

mypy runs in strict mode. All public functions must have type annotations.

**Configuration** (in `pyproject.toml`):

```toml
[tool.mypy]
python_version = "3.12"
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_any_generics = true
check_untyped_defs = true

[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_defs = false  # Test functions don't need full annotations

[[tool.mypy.overrides]]
module = "alembic.*"
ignore_errors = true  # Alembic auto-generated code
```

**Type annotation requirements:**

- All function parameters and return types must be annotated.
- Use `TypedDict` for complex dictionary shapes (not `dict[str, Any]`).
- Use `Protocol` for structural subtyping in service interfaces.
- Agent state schemas must use Pydantic `BaseModel` with fully annotated fields.
- The `Any` type is forbidden except in test code and third-party library wrappers.

```bash
# Run mypy
mypy apps/api/app/ services/reasoning/ services/extraction/ services/rag/
```

### 6.3 TypeScript -- ESLint + Prettier

**ESLint** handles code quality rules. **Prettier** handles formatting. Both share configuration from `packages/config/`.

**ESLint configuration** (`packages/config/eslint-config/`):

```json
{
  "extends": [
    "next/core-web-vitals",
    "plugin:@typescript-eslint/recommended",
    "plugin:@typescript-eslint/recommended-type-checked",
    "prettier"
  ],
  "rules": {
    "@typescript-eslint/no-explicit-any": "error",
    "@typescript-eslint/no-unused-vars": ["error", { "argsIgnorePattern": "^_" }],
    "@typescript-eslint/strict-boolean-expressions": "error",
    "react/jsx-no-leaked-render": "error",
    "import/order": ["error", {
      "groups": ["builtin", "external", "internal", "parent", "sibling", "index"],
      "newlines-between": "always",
      "alphabetize": { "order": "asc" }
    }]
  }
}
```

**Prettier configuration** (`packages/config/.prettierrc`):

```json
{
  "semi": true,
  "singleQuote": false,
  "tabWidth": 2,
  "trailingComma": "all",
  "printWidth": 100,
  "plugins": ["prettier-plugin-tailwindcss"]
}
```

**Commands:**

```bash
# Lint TypeScript
cd apps/web && pnpm lint

# Fix auto-fixable ESLint issues
cd apps/web && pnpm lint --fix

# Format with Prettier
cd apps/web && pnpm exec prettier --write "src/**/*.{ts,tsx,css,json}"

# Check formatting without modifying
cd apps/web && pnpm exec prettier --check "src/**/*.{ts,tsx,css,json}"
```

### 6.4 Pre-Commit Hooks

The project uses `pre-commit` to enforce code quality before every commit.

**Setup:**

```bash
pip install pre-commit
pre-commit install
```

**Hooks that run on every commit:**

| Hook                   | What It Does                                             |
|------------------------|----------------------------------------------------------|
| `ruff check`           | Python lint check (blocks commit on errors).             |
| `ruff format --check`  | Python format check.                                     |
| `mypy`                 | Python type check (on changed files only).               |
| `eslint`               | TypeScript/React lint check.                             |
| `prettier --check`     | TypeScript/CSS/JSON format check.                        |
| `commitlint`           | Validates commit message follows Conventional Commits.   |
| `detect-secrets`       | Scans for accidentally committed secrets/API keys.       |

To bypass hooks in an emergency (never on `main`):

```bash
git commit --no-verify -m "fix: emergency hotfix for X"
```

### 6.5 Import Ordering Conventions

**Python** (enforced by Ruff `I` rules):

```python
# 1. Standard library
import asyncio
from pathlib import Path

# 2. Third-party
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

# 3. First-party (project modules)
from app.services.patient import PatientService
from app.schemas.patient import PatientCreate, PatientResponse
```

**TypeScript** (enforced by ESLint `import/order`):

```tsx
// 1. Node builtins (rare in frontend)
import path from "path";

// 2. External packages
import { useQuery } from "@tanstack/react-query";
import { create } from "zustand";

// 3. Internal packages (monorepo)
import { Button } from "@aether/ui";
import type { Patient } from "@aether/shared-types";

// 4. Parent/sibling imports
import { usePatientStore } from "../stores/patient-store";
import { PatientCard } from "./PatientCard";
```

### 6.6 Naming Conventions

| Context                    | Convention         | Example                                           |
|----------------------------|--------------------|----------------------------------------------------|
| Python functions/variables | `snake_case`       | `get_patient_by_id`, `allergy_count`               |
| Python classes             | `PascalCase`       | `PatientService`, `DrugVocabulary`                 |
| Python constants           | `UPPER_SNAKE_CASE` | `MAX_RETRIES`, `DEFAULT_MODEL`                     |
| Python files/modules       | `snake_case`       | `patient_service.py`, `drug_safety.py`             |
| TypeScript functions/vars  | `camelCase`        | `getPatientById`, `allergyCount`                   |
| React components           | `PascalCase`       | `PatientCard`, `ReasoningTheatre`                  |
| TypeScript types/interfaces| `PascalCase`       | `Patient`, `EncounterResponse`                     |
| TypeScript constants       | `UPPER_SNAKE_CASE` | `MAX_RETRIES`, `API_BASE_URL`                      |
| TypeScript files           | `kebab-case`       | `patient-card.tsx`, `use-patient-store.ts`         |
| CSS classes (Tailwind)     | Tailwind utility   | `className="flex items-center gap-2 text-sm"`      |
| Database tables            | `snake_case`, plural| `patients`, `lab_results`, `clinical_suggestions` |
| Database columns           | `snake_case`       | `patient_id`, `created_at`, `is_deleted`           |
| API endpoints              | `kebab-case`       | `/api/v1/patients`, `/api/v1/drug-safety/check`    |
| Environment variables      | `UPPER_SNAKE_CASE` | `DATABASE_URL`, `ANTHROPIC_API_KEY`                |

---

## 7. Git Workflow and Commit Conventions

### 7.1 Branch Naming

All branches follow the pattern `{type}/{short-description}`:

| Prefix       | Purpose                                    | Example                                  |
|--------------|--------------------------------------------|------------------------------------------|
| `feat/`      | New features                               | `feat/intake-questionnaire`              |
| `fix/`       | Bug fixes                                  | `fix/allergy-cross-check-brand-names`    |
| `chore/`     | Maintenance, dependency updates, CI config | `chore/upgrade-langraph-0.3`             |
| `docs/`      | Documentation-only changes                 | `docs/api-endpoint-reference`            |
| `refactor/`  | Code changes that neither fix bugs nor add features | `refactor/extract-drug-vocabulary-service` |
| `test/`      | Adding or improving tests                  | `test/drug-interaction-edge-cases`       |

Branch names use lowercase with hyphens. Keep them short and descriptive.

### 7.2 Conventional Commits

All commit messages follow the [Conventional Commits](https://www.conventionalcommits.org/) specification. This is enforced by the `commitlint` pre-commit hook.

**Format:**

```
<type>(<optional scope>): <description>

[optional body]

[optional footer(s)]
```

**Types:**

| Type         | When to Use                                           | Example                                           |
|--------------|-------------------------------------------------------|----------------------------------------------------|
| `feat`       | A new feature                                         | `feat(reasoning): add can't-miss sentinel agent`   |
| `fix`        | A bug fix                                             | `fix(drug-safety): resolve brand-to-generic lookup for Dolo-650` |
| `docs`       | Documentation only                                    | `docs: add development guide`                      |
| `test`       | Adding or updating tests                              | `test(agents): add snapshot tests for hypothesis agent output` |
| `chore`      | Build, CI, dependency updates                         | `chore: upgrade pnpm to 9.5`                       |
| `refactor`   | Code change that neither fixes a bug nor adds a feature| `refactor(api): extract patient service from router` |
| `perf`       | Performance improvement                               | `perf(rag): batch Qdrant queries for guideline retrieval` |
| `ci`         | CI/CD changes                                         | `ci: add playwright to GitHub Actions`             |
| `style`      | Code formatting (no logic change)                     | `style: apply ruff format to services/`            |

**Scopes** (optional, but helpful):

`reasoning`, `api`, `web`, `drug-safety`, `extraction`, `rag`, `auth`, `db`, `docker`, `ci`

**Breaking changes** use `!` after the type:

```
feat(api)!: change patient response envelope to use pagination wrapper
```

### 7.3 Pull Request Process

Every PR requires:

1. **Description** -- Use the PR template. Include:
   - What changed and why.
   - Link to the relevant issue/task.
   - Screenshots for UI changes.
   - For agent changes: example input/output demonstrating the change.

2. **Review checklist** (in the PR template):

   ```markdown
   ## Review Checklist
   - [ ] Tests added/updated for the change
   - [ ] No `any` types introduced (TypeScript)
   - [ ] No bare `except:` blocks (Python)
   - [ ] Allergy/contraindication logic is deterministic (no LLM dependency)
   - [ ] Clinical output routes through the Verifier Agent
   - [ ] No imperative clinical language in UI microcopy
   - [ ] ClinicalSuggestion records are never updated/deleted
   - [ ] Evidence appears before conclusions in UI components
   - [ ] Devil's-advocate dissent is visible, not collapsed
   - [ ] Alembic migration is backward-compatible
   - [ ] Environment variables documented in .env.example
   - [ ] `// DESIGN-DECISION:` comments added for spec deviations
   ```

3. **Approval** -- At least one reviewer must approve.

4. **CI passes** -- All checks (lint, type-check, tests, coverage thresholds) must be green.

5. **Squash merge** -- PRs are squash-merged into `main` with a clean conventional commit message.

### 7.4 Main Branch Protection

The `main` branch has the following protections:

- Direct pushes are blocked (all changes go through PRs).
- At least 1 approving review required.
- CI status checks must pass: lint, typecheck, unit tests, integration tests, coverage thresholds.
- Force pushes are disabled.
- Branch must be up to date with `main` before merging.

### 7.5 Release Process

Releases follow semantic versioning (`MAJOR.MINOR.PATCH`):

- **PATCH** (`0.1.1`): Bug fixes, documentation, dependency updates.
- **MINOR** (`0.2.0`): New features, new agents, new API endpoints.
- **MAJOR** (`1.0.0`): Breaking API changes, major architectural shifts, production launch.

Release workflow:

1. Create a release branch from `main`: `release/v0.2.0`.
2. Update version numbers and generate the changelog from conventional commits.
3. Tag the release: `git tag v0.2.0`.
4. Build and push Docker images tagged with the version.
5. Deploy to staging for validation.
6. Merge the release branch back to `main` and deploy to production.

---

## 8. Architecture Patterns for Contributors

These patterns are mandatory for all new code. They keep the codebase consistent and auditable.

### 8.1 Backend: Router -> Service -> Repository

```
Router (thin)          Service (business logic)       Repository (data access)
  |                        |                               |
  | Validates request      | Implements domain rules       | SQLAlchemy queries
  | Calls service          | Orchestrates multiple repos   | Returns model instances
  | Returns response       | Raises domain exceptions      | No business logic
  |                        | Writes audit log entries       |
```

**Rules:**

- **Routers** accept HTTP requests, validate input via Pydantic models, call the service layer, and return responses. Routers contain zero business logic.
- **Services** implement business rules, coordinate across repositories, enforce safety invariants (e.g., allergy checks before medication suggestions), and write audit log entries. Services are injected via FastAPI dependency injection.
- **Repositories** encapsulate all database access via SQLAlchemy. No raw SQL in application code. Repositories return SQLAlchemy model instances, not dictionaries.

```python
# Example: apps/api/app/routers/patients.py (thin router)
@router.post("/", response_model=PatientResponse, status_code=201)
async def create_patient(
    data: PatientCreate,
    service: PatientService = Depends(get_patient_service),
    current_user: User = Depends(get_current_user),
) -> PatientResponse:
    patient = await service.create(data, created_by=current_user.id)
    return PatientResponse.model_validate(patient)
```

### 8.2 Frontend: Page -> Feature Components -> Shared Components

```
Page (apps/web/src/app/)
  |
  +-- Feature Component (data fetching, state, layout)
       |
       +-- Shared Component (packages/ui/, pure presentation)
```

**Rules:**

- **Pages** (`page.tsx` in the App Router) handle routing and top-level layout. They are mostly server components.
- **Feature components** live in `apps/web/src/features/` or `apps/web/src/components/`. They own data fetching (TanStack Query), local state (Zustand stores), and compose shared components.
- **Shared components** live in `packages/ui/src/components/`. They are pure presentation components -- they receive all data via props and emit events via callbacks. No data fetching, no API calls, no stores.

**State management layers:**

| Layer             | Tool             | Purpose                                       |
|-------------------|------------------|------------------------------------------------|
| Server state      | TanStack Query   | API data (patients, encounters, suggestions).  |
| Client state      | Zustand          | UI state (active panel, sidebar collapsed, filter selections). |
| Form state        | React Hook Form  | Form inputs, validation, submission.           |
| URL state         | Next.js params   | Route-driven state (patient ID, encounter ID). |

### 8.3 Agents: Agent Definition -> Tools -> Prompts

Each agent in the reasoning engine follows this structure:

```
services/reasoning/agents/your_agent.py   -- Agent class (LangGraph node)
services/reasoning/prompts/your_agent.md  -- System prompt (Markdown)
services/reasoning/tools/your_tools.py    -- Tool functions the agent can call
services/reasoning/tests/test_your_agent.py -- Tests
```

**Rules:**

- Each agent is a LangGraph node registered in `services/reasoning/graph.py`.
- Agent state extends the shared `CaseState` (or `ReasoningState`) Pydantic model.
- System prompts are stored as Markdown files in `services/reasoning/prompts/`, not as inline strings.
- Tools are plain Python functions decorated with `@tool`. They handle structured operations (database lookups, drug vocabulary queries, guideline retrieval).
- All clinical output from any agent must route through the Verifier Agent. This is verified by integration tests.

### 8.4 Error Handling

**Backend:**

- Define domain-specific exception classes in `app/exceptions.py` (e.g., `PatientNotFoundError`, `AllergyConflictError`, `VerificationFailedError`).
- Raise these exceptions in the service layer.
- Catch them in FastAPI exception handlers (registered in `app/main.py`) to return structured error responses.
- Never swallow exceptions silently. Every `except` block must log the exception.
- Never use bare `except:` or `except Exception:` without re-raising or logging.

```python
# Domain exception
class AllergyConflictError(Exception):
    def __init__(self, drug: str, allergen: str, resolution_path: str):
        self.drug = drug
        self.allergen = allergen
        self.resolution_path = resolution_path
        super().__init__(f"Allergy conflict: {drug} -> {allergen} via {resolution_path}")

# FastAPI exception handler
@app.exception_handler(AllergyConflictError)
async def allergy_conflict_handler(request: Request, exc: AllergyConflictError):
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "type": "allergy_conflict",
                "message": str(exc),
                "details": {
                    "drug": exc.drug,
                    "allergen": exc.allergen,
                    "resolution_path": exc.resolution_path,
                },
            }
        },
    )
```

**Frontend:**

- Error boundaries at the route level catch rendering errors.
- API errors are handled in TanStack Query's `onError` callbacks.
- User-facing errors display as toast notifications with actionable messages.
- Never show raw error messages or stack traces to the user.

### 8.5 Logging

**Backend:**

The project uses `structlog` for structured, JSON-formatted logging with correlation IDs.

```python
import structlog

logger = structlog.get_logger()

async def create_patient(self, data: PatientCreate, created_by: UUID) -> Patient:
    logger.info("creating_patient", patient_name=data.name, created_by=str(created_by))
    patient = await self.repository.create(data)
    logger.info("patient_created", patient_id=str(patient.id))
    return patient
```

**Correlation IDs:**

Every HTTP request receives a unique `correlation_id` (generated in middleware). This ID propagates through all service calls, agent invocations, and database operations. Use it to trace a single request across the entire system.

```python
# Middleware sets correlation_id on the structlog context
@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response
```

**Log levels:**

| Level    | Use For                                                      |
|----------|--------------------------------------------------------------|
| `DEBUG`  | Detailed diagnostic info (agent intermediate states, SQL queries). |
| `INFO`   | Key events (patient created, reasoning session started, document extracted). |
| `WARNING`| Recoverable issues (low extraction confidence, API rate limit approaching). |
| `ERROR`  | Failures that need attention (LLM call failed, database connection lost). |

### 8.6 Database

- All database access goes through **SQLAlchemy ORM** (async). No raw SQL in application code.
- Schema changes are managed by **Alembic** migrations. Never modify the database directly.
- The `clinical_suggestions` table is **append-only** (enforced by a PostgreSQL trigger, the ORM model, and migration policy). See `CLAUDE.md` for full safety rules.
- Use UUIDv4 primary keys (`gen_random_uuid()`).
- All timestamps are `TIMESTAMPTZ` in UTC. Convert to IST in the application/presentation layer only.

### 8.7 DESIGN-DECISION Comments

When the implementation intentionally deviates from the specification documents, mark it with a `// DESIGN-DECISION:` comment (Python: `# DESIGN-DECISION:`) explaining the what and why:

```python
# DESIGN-DECISION: Using synchronous Tesseract call here instead of async.
# The pytesseract library does not support async natively, and wrapping it in
# run_in_executor adds complexity for a fallback path that runs infrequently.
# Revisit if OCR fallback becomes a hot path.
result = pytesseract.image_to_string(image, lang="eng+hin")
```

These comments create a searchable record of design decisions and make code review faster by preemptively answering "why did you do it this way?"

---

## 9. Deployment Process

### 9.1 Local (Docker Compose)

For local development, Docker Compose runs only infrastructure services. Application code runs natively for hot reload.

```bash
# Infrastructure only (default)
docker compose up -d

# Full stack in Docker (no hot reload)
docker compose --profile full up -d
```

### 9.2 Staging

Staging mirrors production with a dedicated Docker Compose stack or a single-server deployment.

```bash
# Build Docker images
docker compose -f docker-compose.staging.yml build

# Deploy
docker compose -f docker-compose.staging.yml up -d

# Run migrations on the staging database
docker compose -f docker-compose.staging.yml exec api alembic upgrade head
```

Staging uses a separate PostgreSQL database, a separate Qdrant collection, and a staging Anthropic API key (same model, separate billing).

### 9.3 Production

Production deployment targets India-region infrastructure (DPDP Act compliance). All patient data stays in India.

```bash
# Build production images
docker build -t aether-api:v0.2.0 -f apps/api/Dockerfile .
docker build -t aether-web:v0.2.0 -f apps/web/Dockerfile .
docker build -t aether-reasoning:v0.2.0 -f services/reasoning/Dockerfile .

# Push to container registry
docker push registry.example.com/aether-api:v0.2.0
docker push registry.example.com/aether-web:v0.2.0
docker push registry.example.com/aether-reasoning:v0.2.0
```

**Production checklist:**

- [ ] `APP_ENV=production` and `APP_DEBUG=false`
- [ ] `DEMO_MODE=false` (unless deploying a demo instance)
- [ ] `APP_SECRET_KEY` is a unique, randomly generated secret
- [ ] `DATABASE_URL` points to the production PostgreSQL instance (India region)
- [ ] `S3_REGION=ap-south-1` (data residency)
- [ ] All feature flags reviewed and set intentionally
- [ ] `FF_DEVILS_ADVOCATE=true` (cannot be disabled in production)
- [ ] TLS enabled via Nginx reverse proxy
- [ ] Database migrations applied and verified
- [ ] Health check endpoint (`/health`) returns 200

### 9.4 Database Migration Safety

All Alembic migrations must be **backward-compatible**. This means the old application version can run against the new schema during a rolling deploy.

**Rules:**

- Adding a column: must have a default value or be nullable.
- Removing a column: deploy code that stops reading the column first, then remove the column in a subsequent migration.
- Renaming a column: never. Create the new column, migrate data, update code, then drop the old column in a later migration.
- Adding an index: use `CREATE INDEX CONCURRENTLY` (in the Alembic migration's `op.execute()`) to avoid table locks.
- Never add `UPDATE` or `DELETE` grants on the `clinical_suggestions` table.

### 9.5 Health Checks and Rollback

**Health check endpoint:** `GET /health`

Returns the status of all dependencies:

```json
{
  "status": "healthy",
  "version": "0.2.0",
  "checks": {
    "database": "ok",
    "redis": "ok",
    "qdrant": "ok",
    "storage": "ok"
  }
}
```

**Rollback procedure:**

1. Roll back the application to the previous Docker image tag.
2. If the migration is backward-compatible (it should be), the old code runs against the new schema.
3. If a migration must be rolled back: `alembic downgrade -1` (test this in staging first).
4. Never roll back a migration that created the immutability trigger on `clinical_suggestions`.

---

## 10. Common Tasks

### 10.1 Adding a New API Endpoint

1. Create a router file: `apps/api/app/routers/your_resource.py`.
2. Define Pydantic request/response models: `apps/api/app/schemas/your_resource.py`.
3. Implement business logic in a service: `apps/api/app/services/your_service.py`.
4. Add repository methods if new database queries are needed: `apps/api/app/repositories/your_repo.py`.
5. Register the router in `apps/api/app/main.py`:
   ```python
   from app.routers import your_resource
   app.include_router(your_resource.router, prefix="/api/v1/your-resource", tags=["your-resource"])
   ```
6. If the endpoint touches clinical data, add audit log entries in the service layer.
7. Write tests in `apps/api/tests/test_your_resource.py`.
8. Update shared types in `packages/shared-types/` if the frontend needs the new shapes.
9. Document the endpoint in the API specification (`docs/api-design.md`).

### 10.2 Adding a New UI Screen

1. Create the page: `apps/web/src/app/(routes)/your-page/page.tsx`.
2. Create feature components in `apps/web/src/features/your-feature/`.
3. Add shared components to `packages/ui/src/components/` if reusable.
4. Use types from `packages/shared-types/` for API response typing.
5. Add API hooks using TanStack Query in `apps/web/src/lib/api/`.
6. If the screen displays clinical output, verify:
   - Evidence is shown before conclusions (anti-automation-bias).
   - Devil's-advocate dissent is visible and not collapsed.
   - Autonomy tier badges are displayed.
   - No certainty language in microcopy ("Guidelines support considering..." not "Give drug X").
7. Add the route to navigation.
8. Write component tests and add the flow to E2E tests.

### 10.3 Adding a New Agent to the Reasoning Engine

1. Create the agent module: `services/reasoning/agents/your_agent.py`.
2. Define the agent's state schema extending `ReasoningState`.
3. Write the system prompt: `services/reasoning/prompts/your_agent.md`.
4. Define any tools the agent needs: `services/reasoning/tools/your_tools.py`.
5. Register the agent as a node in the graph: `services/reasoning/graph.py`.
6. Define edges (which agents run before/after) in the graph.
7. Add the agent's output to the Verifier's checklist -- all clinical output must be verified.
8. Update the Synthesis/Orchestrator to incorporate the new agent's output.
9. Write unit tests: `services/reasoning/tests/test_your_agent.py`.
10. Write an integration test confirming the agent's output routes through the Verifier.
11. Validate graph structure: `python -m reasoning.graph --dry-run`.

### 10.4 Adding a New Drug to the Vocabulary

1. Add entries to `data/drugs/` seed data (CSV format: `brand_name,generic_name_inn,reference_id,manufacturer,strength`).
2. Map Indian brand name to generic name (INN) to reference ID.
3. Run the seed script: `python -m scripts.seed_drugs`.
4. Verify allergy and contraindication cross-references resolve correctly by running drug safety tests:
   ```bash
   pytest apps/api/tests/test_drug_safety.py -v
   ```
5. If the drug has known interactions, add them to the interaction reference data.

### 10.5 Adding a New Guideline to the Corpus

1. Place the source document (PDF, Markdown, or structured text) in `data/guidelines/`.
2. Add metadata (guideline name, source organization, version, specialty) to the guideline manifest.
3. Run the ingestion pipeline:
   ```bash
   python -m services.rag.ingest --source data/guidelines/your_guideline.pdf
   ```
4. Each chunk retains its source citation metadata (guideline name, section, page number).
5. Verify retrieval quality with test queries:
   ```bash
   python -m services.rag.test_retrieval --query "management of type 2 diabetes"
   ```
6. Add regression test queries to `services/rag/tests/`.

### 10.6 Running a Single Test File

```bash
# Python
pytest apps/api/tests/test_patient_service.py -v

# TypeScript (Vitest)
cd apps/web && pnpm test -- src/components/PatientCard.test.tsx

# E2E (Playwright)
cd apps/web && pnpm exec playwright test e2e/patient-intake.spec.ts
```

### 10.7 Debugging Agent Behavior (Trace Inspection)

The reasoning engine generates full traces of agent execution. To inspect them:

```bash
# Run reasoning with trace output (writes to stdout and logs)
python -m reasoning.run --patient-id <uuid> --trace

# Dry-run the graph (validates structure, no LLM calls)
python -m reasoning.graph --dry-run

# Inspect a specific agent's intermediate state
python -m reasoning.inspect --session-id <uuid> --agent hypothesis_panel
```

In the web UI, the Reasoning Theatre displays the full agent trace in real time. Each agent panel shows:
- Input state the agent received.
- The agent's output (hypotheses, counter-arguments, recommendations).
- The Verifier's assessment and autonomy tier classification.
- Timing information (how long each agent took).

### 10.8 Resetting the Local Database

```bash
# Full reset: drop database, recreate, migrate, seed
python -m scripts.reset_db

# Or manually:
docker compose exec postgres psql -U aether -c "DROP DATABASE aether_clinician;"
docker compose exec postgres psql -U aether -c "CREATE DATABASE aether_clinician;"
alembic upgrade head
python -m scripts.seed_drugs
python -m scripts.seed_guidelines
```

---

## 11. Troubleshooting

### 11.1 Common Setup Issues

| Problem                                         | Solution                                                                                                                                      |
|--------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| `python3.12: command not found`                  | Install Python 3.12 via `pyenv install 3.12` and set it with `pyenv local 3.12`.                                                             |
| `pnpm: command not found`                        | Install pnpm globally: `npm install -g pnpm@latest`.                                                                                          |
| `docker compose: command not found`              | You may have the legacy v1 `docker-compose`. Install Docker Desktop (includes Compose v2) or install the Compose v2 plugin.                   |
| Alembic migration fails with "relation already exists" | The database has a stale schema. Reset with `python -m scripts.reset_db` or `alembic downgrade base && alembic upgrade head`.              |
| `ModuleNotFoundError: No module named 'app'`     | Ensure the virtual environment is activated and you installed with `pip install -e ".[dev]"` (the `-e` flag makes the project editable).       |
| Tesseract `TesseractNotFoundError`               | Install Tesseract (`brew install tesseract` on macOS) and verify the path in `TESSERACT_CMD`. macOS Homebrew: `/opt/homebrew/bin/tesseract`.   |
| MinIO bucket not found                           | Run the seed script (`python -m scripts.seed_demo_user`) or create the bucket manually via the MinIO console at `http://localhost:9001`.      |
| Port 5432 already in use                         | You have a local PostgreSQL instance running. Stop it (`brew services stop postgresql`) or change the port in `docker-compose.yml`.           |
| `pnpm install` fails with peer dependency errors | Run `pnpm install --no-strict-peer-dependencies` or update the conflicting package.                                                           |

### 11.2 Claude API Rate Limits and Errors

| Error                                | Cause                                       | Solution                                                                                                       |
|--------------------------------------|---------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| `429 Too Many Requests`              | Rate limit exceeded.                        | The backend includes exponential backoff with jitter. If you hit this in development, slow down your testing loop. Check your Anthropic console for usage tier limits. |
| `401 Unauthorized`                   | Invalid or expired API key.                 | Regenerate your API key at [console.anthropic.com](https://console.anthropic.com) and update `.env`.            |
| `529 Overloaded`                     | Anthropic servers under load.               | Retry after a short delay. The backend retries automatically with backoff.                                      |
| `400 Bad Request` with model errors  | Model name mismatch or unsupported feature. | Verify `ANTHROPIC_MODEL` in `.env` matches a valid model ID. Check the Anthropic changelog for API changes.     |
| Reasoning session times out          | Full 8-agent pipeline can take 30-60s.      | This is expected for complex cases. The frontend shows streaming progress via SSE. Increase timeout if needed.   |

**Developing without an API key:** Set `FF_OFFLINE_MODE=true` to disable LLM features. CRUD operations, drug safety checks (deterministic), and UI development work normally. Agent tests use mocked LLM responses.

### 11.3 Database Migration Conflicts

| Problem                                      | Solution                                                                                      |
|----------------------------------------------|-----------------------------------------------------------------------------------------------|
| Two developers create migrations concurrently | Alembic will detect a branch. Resolve by running `alembic merge heads -m "merge migrations"`. |
| Migration fails on staging but works locally | Check for differences in database state. Run `alembic history` to compare migration chains.   |
| "Target database is not up to date"          | Run `alembic upgrade head` to apply pending migrations.                                       |
| Need to undo a migration                     | `alembic downgrade -1` rolls back the most recent migration. Test this locally first.          |
| Auto-generated migration is empty            | Alembic did not detect changes. Check that your model changes are imported in `env.py`.        |

### 11.4 Docker Networking Issues

| Problem                                       | Solution                                                                                                       |
|------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| Application cannot connect to PostgreSQL       | Verify the container is running (`docker compose ps`). Check `DATABASE_URL` uses `localhost` (not the container name) when running the app outside Docker. |
| Container-to-container communication fails     | When running the app inside Docker, use service names (`postgres`, `redis`, `qdrant`) instead of `localhost`.   |
| Qdrant connection refused                      | Qdrant may take 10-15 seconds to start. Wait and retry. Check logs: `docker compose logs qdrant`.              |
| Redis connection refused after Docker restart  | Redis data is ephemeral in development. Restart the app to reconnect.                                           |
| `docker compose up` hangs                      | Check available disk space and memory. Docker Desktop may need more resources allocated in Settings.             |

### 11.5 Offline Mode Testing

To test the system's offline behavior (core safety checks without LLM features):

```bash
# Set offline mode
export FF_OFFLINE_MODE=true

# Or in .env
FF_OFFLINE_MODE=true
```

In offline mode:
- Patient CRUD operations work normally.
- Document upload works (stored locally), but extraction requires manual data entry.
- Drug safety checks (allergy, contraindication, interaction) work -- they are deterministic and do not depend on the LLM.
- The reasoning engine shows an "AI reasoning paused -- offline" indicator.
- Guideline RAG is unavailable (depends on Qdrant, which depends on the network for initial loading but works locally once loaded).

**Test these specific behaviors in offline mode:**

1. Create a patient with a known allergy (e.g., Sulfonamides).
2. Attempt to prescribe a conflicting drug (e.g., Sulfasalazine). Verify the hard block fires.
3. Verify the UI shows "AI reasoning paused -- offline" without errors.
4. Verify no LLM API calls are attempted (check logs for Anthropic API errors -- there should be none).

---

*This is a living document. Update it when you add new tooling, change the setup process, or discover new troubleshooting patterns. If a new contributor hits an issue not covered here, the fix belongs in this guide.*
