# Documedic / Aether Clinician — PROJECT_INFO

**Clinician-facing diagnostic and management decision-support system (CDSS) for primary care in India — turns fragmented histories into a structured longitudinal record, runs offline-capable drug-safety checks, and keeps a hash-chained audit trail.**

> Demo build — decision-support only, not for real patient care. The clinician always decides.

- **Repo:** https://github.com/r2st/documedic · branch `main`
- **Local path:** `Products/Documedic`
- **Status:** all four phases implemented (foundation · multi-agent reasoning + Reasoning Theatre · guideline RAG · clinical validation/regulatory)

## Tech stack

| Layer | Technology |
|---|---|
| Monorepo | Turborepo — `apps/{api,web}`, `packages/`, `services/{rag,reasoning}` |
| API | Python (FastAPI) — `apps/api/app/` |
| Web | Node/TypeScript SPA |
| Database | PostgreSQL |
| Cache/queue | Redis |
| Vector store | Qdrant |
| Object storage | MinIO (S3-compatible, content-addressed by SHA-256) |
| LLM | Claude (vision extraction) via OpenRouter, plus an OpenAI/GPT key |
| OCR fallback | Tesseract + a deterministic parser when no API key is present |
| Auth | bcrypt + JWT access tokens, opaque hashed refresh tokens with rotation |
| Proxy | nginx (`nginx/`) |

## Deploy location

| | |
|---|---|
| Host | Hetzner `89.167.8.178` — shared with Herald, GoSumo, TalentPing, HomeNex, Knol |
| Ports on the box | `3003` / `3004` (per Herald's port map for the shared host) |
| Public URL | `documedic.aiknol.com` |
| Local compose ports | `8000` api · `3000` web · `5432` postgres · `6379` redis · `6333` qdrant · `9000`/`9001` minio |
| Ingress | The box's shared Caddy container |

> The server code path and the exact service/unit names are not recorded in this repo.
> Read them off the box and fill in here.

## SSH key

`keys/hetzner_deploy_ed25519` (+ `.pub`); host IP at `keys/hetzner_vps_ip`.

```bash
ssh -i keys/hetzner_deploy_ed25519 root@89.167.8.178
```

> Shared across five projects — see `~/projects/keys/KEYS_INDEX.md` §4.

## Environment variables

| Where | What |
|---|---|
| `.env` (gitignored) | Local dev; full var table in [`docs/development-guide.md`](docs/development-guide.md) |
| `keys/` (gitignored) | `openrouter-key`, `llm-gpt-key.txt`, `sendgrid.txt`, `Cloudfare_token.txt`, `Git_token.txt`, `hetzner_*` |

Notable var: `TESSERACT_CMD` — path to the Tesseract binary (`/opt/homebrew/bin/tesseract`
on macOS, `/usr/bin/tesseract` on Linux). Without it, extraction falls back to the
deterministic parser.

⚠️ This repo's `origin` remote has a **GitHub token embedded in the URL**. Rotate it and
switch to a credential helper — see `~/projects/keys/KEYS_INDEX.md` §5.

## Key commands

```bash
npm install
docker compose up -d           # postgres, redis, qdrant, minio, api, web
npm run dev                    # turbo dev
npm run build
npm test                       # turbo test
npm run typecheck && npm run lint
```

Compliance/design detail: `docs/implementation-plan.md`, `docs/development-guide.md`, `CLAUDE.md`.

## Related projects

- [`../Herald`](../Herald), [`../GoSumo`](../GoSumo), [`../TalentPing`](../TalentPing), [`../HomeNex`](../HomeNex) — same Hetzner box, **same SSH deploy key**
- `knol/memorylayer` — backs the `*.aiknol.com` estate and the shared Caddy container
- `~/projects/PROJECT-INDEX.md`, `~/projects/keys/KEYS_INDEX.md` — estate-wide index
