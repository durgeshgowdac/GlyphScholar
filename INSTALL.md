# Installation Guide

This guide covers the full local setup of GlyphScholar. For a project overview and architecture, see [README.md](./README.md). Deploying to production instead? See [hosting.md](./hosting.md) — it covers Vercel, Render, Neon, and Backblaze B2, none of which are needed for local dev.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Hardware Requirements](#hardware-requirements)
- [Software Requirements](#software-requirements)
- [Quick Start](#quick-start)
- [Detailed Setup](#detailed-setup)
  - [1. Database (PostgreSQL + pgvector)](#1-database-postgresql--pgvector)
  - [2. Ollama (Local Model Fallback)](#2-ollama-local-model-fallback)
  - [3. Supabase](#3-supabase)
  - [4. MinerU](#4-mineru)
  - [5. Hugging Face](#5-hugging-face)
  - [6. Modal](#6-modal)
  - [7. Opik](#7-opik)
  - [8. Chainlit](#8-chainlit)
  - [9. Database Migrations](#9-database-migrations)
- [Running the Application](#running-the-application)
- [Environment Variable Reference](#environment-variable-reference)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Hardware Requirements

| Component | Requirement |
|---|---|
| Local development machine | macOS (this guide uses Homebrew throughout) |
| Cloud inference (Modal) | Serverless GPU containers — defaults to **NVIDIA A10G** for embedding, reranking, and answer generation. Configurable via `MODAL_EMBED_GPU`, `MODAL_RERANK_GPU`, `MODAL_ANSWER_GPU` |
| Database | PostgreSQL 18 with the `pgvector` extension, hosted locally for dev (or on Neon in production — see hosting.md). Supabase runs a *separate* Postgres of its own, used only for auth (`auth.users`/`profiles`), not for this database. |

### Software Requirements

| Software / service | Purpose |
|---|---|
| [PostgreSQL 18](https://www.postgresql.org/) | Primary datastore for application data and chat history |
| [pgvector](https://github.com/pgvector/pgvector) | Postgres extension for vector similarity search (embeddings) |
| [Ollama](https://ollama.com/) | Local inference for `qwen3-vl:2b-instruct` (answering) and `nomic-embed-text` (embeddings), used as a fallback path |
| [Supabase](https://supabase.com/) (CLI + hosted project) | Hosted Postgres, project credentials, and the Chainlit data layer |
| [Modal](https://modal.com/) | Serverless GPU hosting for the Qwen3-VL embedding, reranker, and answer models. (`torch`, `sentence-transformers`, and `huggingface_hub` run inside Modal's own container image, not locally.) |
| [Chainlit](https://docs.chainlit.io/) | Chat UI framework mounted inside the FastAPI backend |
| [MinerU](https://mineru.net/) | Hosted PDF parsing API (layout, OCR, tables, formulas) — no local installation required |
| [Opik](https://www.comet.com/site/products/opik/) | LLM tracing and observability |
| [Hugging Face](https://huggingface.co/settings/tokens) | Model weight downloads (`HF_TOKEN`), used by Modal |
| Python (FastAPI, asyncpg, httpx, openai, rank-bm25, PyMuPDF, PyJWT, aiofiles) | Backend runtime — see `requirements.txt` for the complete list |
| Node.js / npm | Runs the `portal` (Next.js) frontend and root dev scripts |

You will also need accounts for: **Supabase, Modal, MinerU, Hugging Face,** and **Opik**.

#### Verified versions

Homebrew installs the current release of most tools automatically, so exact numbers will drift over time — pin only where the project depends on a specific major version (Python 3.12, PostgreSQL 18). Use the check command to confirm what's installed.

| Software | Version used in this guide | Check installed version |
|---|---|---|
| Python | 3.12 (developed on 3.12.10) | `python3.12 --version` |
| Node.js | 26.x LTS (e.g. 26.8.2) | `node --version` |
| npm | Bundled with Node 26 (e.g. 11.19.1) | `npm --version` |
| Git | Any recent version | `git --version` |
| Homebrew | Any recent version | `brew --version` |
| PostgreSQL | 18.x (e.g. 18.6) | `psql --version` |
| pgvector | 0.8.x (Homebrew formula, built against `postgresql@18`) | `psql -d glyphscholar -c "SELECT extversion FROM pg_extension WHERE extname='vector';"` |
| Ollama | Current stable (`brew upgrade ollama` to update) | `ollama --version` |
| Supabase CLI | Current stable (rolling weekly releases) | `supabase --version` |
| Modal (Python) | `1.5.5` (pinned in `requirements.txt`) | `pip show modal` |
| Chainlit (Python) | `2.12.0` (pinned in `requirements.txt`) | `chainlit --version` |
| FastAPI (Python) | `0.141.1` (pinned in `requirements.txt`) | `pip show fastapi` |
| Opik (Python) | `~2.2.72` (pinned in `requirements.txt`) | `pip show opik` |

> Ollama and the Supabase CLI ship frequent point releases, so no single version number stays current for long — treat the check commands above as the source of truth, not this table.

---

## Quick Start

For readers who want the commands up front. Each step is explained in detail in [Detailed Setup](#detailed-setup) below.

```bash
# 1. Clone and configure
git clone https://github.com/durgeshgowdac/GlyphScholar.git glyphscholar
cd glyphscholar
cp .env.example .env

# 2. Install dependencies
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install --prefix portal
npm install

# 3. Database
brew install postgresql@18 pgvector
brew services start postgresql@18

# 4. Local model fallback
brew install ollama
brew services start ollama
ollama pull qwen3-vl:2b-instruct
ollama pull nomic-embed-text

# 5. Run migrations (after filling in .env — see Detailed Setup)
psql -d postgres -f backend/migrations/000_bootstrap.sql
psql "postgresql://glyphuser:glyphpass@localhost:5432/glyphscholar" \
  -f backend/migrations/001_app_schema.sql \
  -f backend/migrations/002_chainlit_datalayer.sql

# 6. Run the app
npm run dev
```

The Supabase, MinerU, Hugging Face, Modal, Opik, and Chainlit credentials referenced above must be configured in `.env` before the app will run correctly — follow the detailed steps below.

---

## Detailed Setup

### 1. Database (PostgreSQL + pgvector)

```bash
brew install postgresql@18
brew services start postgresql@18

# pgvector — required by backend/migrations/001_app_schema.sql (CREATE EXTENSION "vector")
brew install pgvector
```

### 2. Ollama (Local Model Fallback)

```bash
brew install ollama
brew services start ollama

ollama pull qwen3-vl:2b-instruct
ollama pull nomic-embed-text
```

These models correspond to `OLLAMA_ANSWER_MODEL` and `OLLAMA_EMBED_MODEL` in `.env`. Ollama is used whenever `USE_MODAL_EMBED`, `USE_MODAL_RERANK`, or `USE_MODAL_ANSWER` are set to `False`.

### 3. Supabase

```bash
brew install supabase
supabase login
supabase link --project-ref YOUR_PROJECT_REF
supabase db push
```

In your Supabase project dashboard, copy the following into `.env`:

| Dashboard location | `.env` variable |
|---|---|
| Settings → Data API → Project URL | `SUPABASE_URL` |
| Settings → API → service_role key | `SUPABASE_SERVICE_ROLE_KEY` |

### 4. MinerU

1. Create an account at [mineru.net](https://mineru.net) and generate an API key under **API Keys**.
2. Set the following in `.env`:

   ```
   MINERU_TOKEN="your-mineru-token"
   MINERU_BASE_URL="https://mineru.net/api/v4"
   MINERU_MODEL_VERSION=vlm
   MINERU_IS_OCR=false
   MINERU_ENABLE_TABLE=true
   MINERU_ENABLE_FORMULA=true
   MINERU_LANGUAGE=en
   MINERU_POLL_INTERVAL=10
   MINERU_MAX_POLL_TIME=1800
   ```

   `MINERU_MODEL_VERSION=vlm` selects MinerU's vision-language parsing pipeline — see MinerU's docs for other supported values. Optional overrides (output directory, timeouts, retries) are documented inline in `.env.example`.

### 5. Hugging Face

1. Generate a **Read** token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens). A read-only token is sufficient — it's used only to download public model weights.
2. Set `HF_TOKEN` in `.env`.

### 6. Modal

`modal` is already installed via `requirements.txt`. Authenticate:

```bash
modal setup
```

Push `MODAL_API_KEY` (generate with `openssl rand -hex 32`) and `HF_TOKEN` into a Modal Secret. The deployed `EmbeddingServer`, `RerankServer`, and `AnswerServer` all read these from the environment:

```bash
modal secret create --force glyphscholar-secret \
  MODAL_API_KEY="$(grep MODAL_API_KEY .env | cut -d= -f2)" \
  HF_TOKEN="$(grep HF_TOKEN .env | cut -d= -f2)"
```

Download model weights into a Modal Volume (one-time, ~10 GB):

```bash
modal run modal/services.py::download_models
```

Deploy the services:

```bash
modal deploy modal/services.py
```

Useful commands:

```bash
modal app logs glyphscholar   # Tail logs from the deployed app (app name defined in modal/services.py)
modal run modal/services.py   # Run the smoke test against live containers
```

Set `USE_MODAL_EMBED=True`, `USE_MODAL_RERANK=True`, and `USE_MODAL_ANSWER=True` in `.env` to route inference through Modal instead of Ollama. GPU type per service defaults to A10G and is configurable via `MODAL_EMBED_GPU`, `MODAL_RERANK_GPU`, `MODAL_ANSWER_GPU`.

### 7. Opik

1. Create an account/workspace at [comet.com/opik](https://www.comet.com/site/products/opik/) and generate an API key.
2. Set the following in `.env`:

   ```
   OPIK_API_KEY="your-opik-api-key"
   OPIK_WORKSPACE="glyphscholar"
   OPIK_PROJECT_NAME="GlyphScholar"
   ```

### 8. Chainlit

`chainlit` is already installed via `requirements.txt`. Generate an auth secret:

```bash
chainlit create-secret
```

Copy the generated value into `CHAINLIT_AUTH_SECRET` in `.env`.

### 9. Database Migrations

```bash
psql -d postgres -f backend/migrations/000_bootstrap.sql

psql "postgresql://glyphuser:glyphpass@localhost:5432/glyphscholar" \
  -f backend/migrations/001_app_schema.sql \
  -f backend/migrations/002_chainlit_datalayer.sql
```

| Migration | Purpose |
|---|---|
| `000_bootstrap.sql` | Creates the `glyphuser` role and the `glyphscholar` database |
| `001_app_schema.sql` | Enables the `pgcrypto` and `vector` (pgvector) extensions and creates the application schema |
| `002_chainlit_datalayer.sql` | Creates the tables Chainlit needs for chat/session persistence |

Confirm that `DATABASE_URL` in `.env` matches the connection string used above.

---

## Running the Application

Run the portal (frontend) and backend together:

```bash
npm run dev
```

Or run each independently:

```bash
npm run portal    # npm run dev --prefix portal, serves http://localhost:3000
npm run backend   # uvicorn backend.server:app --reload --host 0.0.0.0 --port 8000
```

The FastAPI backend mounts the Chainlit app and serves the API; the portal is the frontend, proxied to it during development.

---

## Environment Variable Reference

All variables live in `.env` (copied from `.env.example`), grouped by section:

| Section | Variables |
|---|---|
| App / CORS | `ENVIRONMENT`, `PORTAL_PORT`, `CORS_ORIGINS`, `PDF_RENDER_QUOTA_MULTIPLIER` |
| Auth & Security | `CHAINLIT_AUTH_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` |
| Database | `DATABASE_URL`, `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE` |
| Storage (production only, not in `.env.example`) | `B2_BUCKET`, `B2_ENDPOINT`, `B2_KEY_ID`, `B2_APP_KEY`, `B2_REGION` — only read when `ENVIRONMENT=production`; see hosting.md § Storage |
| Ingestion | `RENDER_CONCURRENCY`, `CHUNK_SIZE`, `CHUNK_OVERLAP`, `MAX_PDF_PAGES` |
| MinerU | `MINERU_TOKEN`, `MINERU_BASE_URL`, `MINERU_MODEL_VERSION`, `MINERU_IS_OCR`, `MINERU_ENABLE_TABLE`, `MINERU_ENABLE_FORMULA`, `MINERU_LANGUAGE`, `MINERU_POLL_INTERVAL`, `MINERU_MAX_POLL_TIME` (plus optional overrides in `.env.example`) |
| Model selection | `USE_MODAL_EMBED`, `USE_MODAL_RERANK`, `USE_MODAL_ANSWER`, `EMBED_MODEL`, `RERANK_MODEL`, `ANSWER_MODEL`, `EMBED_DIM` |
| Modal | `MODAL_API_KEY`, `HF_TOKEN`, `MODAL_EMBED_GPU`, `MODAL_RERANK_GPU`, `MODAL_ANSWER_GPU`, `RERANK_TOP_N`, `TEXT_FALLBACK_DISTANCE`, `PIXEL_FALLBACK_DISTANCE`, `MODAL_MAX_CONTEXT_TOKENS`, `MODAL_MAX_RESPONSE_TOKENS`, `MODAL_FINAL_TOP_K`, `MODAL_DENSE_TOP_K`, `MODAL_PIXEL_TOP_K` |
| Ollama | `OLLAMA_BASE_URL`, `OLLAMA_ANSWER_MODEL`, `OLLAMA_EMBED_MODEL`, `OLLAMA_MAX_CONTEXT_TOKENS`, `OLLAMA_MAX_RESPONSE_TOKENS`, `OLLAMA_NUM_CTX`, `OLLAMA_FINAL_TOP_K`, `OLLAMA_DENSE_TOP_K`, `OLLAMA_PIXEL_TOP_K` |
| Opik | `OPIK_API_KEY`, `OPIK_WORKSPACE`, `OPIK_PROJECT_NAME` |

---

## Project Structure

```
.
├── backend/
│   ├── server.py              # FastAPI app, mounts Chainlit
│   ├── db.py                  # Postgres/asyncpg access layer
│   ├── ingest.py               # PDF ingestion pipeline
│   ├── mineru_client.py        # MinerU API client
│   ├── retrieve.py             # Dense/BM25/pixel retrieval + reranking
│   ├── storage.py              # Upload/file storage helpers
│   ├── rag_log.py              # Opik logging helpers
│   ├── public/                 # Favicon served at /chat/favicon
│   └── migrations/             # SQL migrations (bootstrap, schema, Chainlit data layer)
├── chainlit_app/
│   └── app.py                  # Chainlit chat UI logic
├── modal/
│   └── services.py             # Embedding/Rerank/Answer servers deployed on Modal
├── portal/                     # Next.js frontend (auth pages, landing page, Supabase client)
├── supabase/                   # Supabase project config + migrations
├── public/                     # Chainlit theme, logos, avatars (auto-loaded by Chainlit)
├── .chainlit/                  # Chainlit config + translations
├── .env.example
├── requirements.txt
└── package.json
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `CREATE EXTENSION "vector"` fails during migrations | `pgvector` isn't installed, or was built against a different Postgres version than `postgresql@18`. Re-run `brew install pgvector` after confirming `postgresql@18` is the active formula. |
| `modal setup` or `modal deploy` fails with an auth error | Run `modal setup` again to re-authenticate, or check that `MODAL_API_KEY` in `.env` matches the value pushed to the `glyphscholar-secret` Modal Secret. |
| Ingestion requests to MinerU time out or hang | Large PDFs can exceed `MINERU_MAX_POLL_TIME` (default 1800s). Increase it in `.env`, or check MinerU's dashboard for job status. |
| Chainlit UI loads but chat history doesn't persist | Confirm `002_chainlit_datalayer.sql` ran successfully and that `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` are set correctly. |
| `npm run dev` fails to start the portal | Ensure `npm install --prefix portal` completed without errors, and that `portal/.env.local` (or `.env.local.example` copied over) has the required Supabase client keys. |
| Port `3000` or `8000` already in use | Stop any other process bound to those ports, or change `PORTAL_PORT` in `.env` and the `--port` flag in the `backend` script in `package.json`. |