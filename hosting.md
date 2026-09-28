# Hosting

This document covers **production deployment** — where each piece of GlyphScholar actually runs, and the environment variables each host needs. For running everything on one machine, see [INSTALL.md](./INSTALL.md) instead.

## At a glance

| Component | Host | Notes |
|---|---|---|
| Auth (users, sessions, JWT issuing) | **Supabase** | Also hosts its own Postgres, used *only* for `auth.users` / `profiles` — see [Two databases, not one](#two-databases-not-one) |
| Portal (Next.js frontend) | **Vercel** | Project root: `portal/` |
| Backend (FastAPI + Chainlit) | **Render** | Web service; `npm start` |
| App database (`DATABASE_URL`) | **Local Postgres** or **Neon** | Same connection string either way — see [Database](#database-local-or-neon) |
| Chat/element storage (production) | **Backblaze B2** | Via Chainlit's S3-compatible storage client |
| PDF parsing | **MinerU** | Hosted API — no infra to run, just an API key |
| LLM tracing | **Opik** | Hosted (comet.com) — API key only |
| Embedding / rerank / answer inference | **Modal** | Serverless GPU functions, deployed independently of Render/Vercel |
| Local inference fallback | **Ollama** | Dev-only — not deployed anywhere in production |

```mermaid
flowchart LR
    Browser -->|portal pages, login/signup| Vercel[Vercel: portal/]
    Browser -->|/chat, uploads| Render[Render: backend + chainlit_app]
    Vercel -->|/auth/resolve-login, /auth/bridge| Render
    Render -->|verify JWT via JWKS| Supabase[(Supabase: auth.users)]
    Render -->|DATABASE_URL| AppDB[(Local Postgres or Neon:<br/>document_chunks, app_users, chat_sessions, chainlit tables)]
    Render -->|chat element storage| B2[(Backblaze B2)]
    Render -->|PDF parsing| MinerU[MinerU API]
    Render -->|inference| Modal[Modal GPU functions]
    Render -->|trace logging| Opik[Opik]
```

## Auth — Supabase

Supabase is used purely as the auth provider: it issues and verifies the session JWT, and hosts one small Postgres schema (`schema.sql`) for `auth.users`, `profiles`, and the `get_email_from_username` RPC that backs username-based login. It does **not** host the application's own data — see below.

Set on **Render**:

| Variable | Where to find it |
|---|---|
| `SUPABASE_URL` | Supabase dashboard → Settings → Data API → Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase dashboard → Settings → API → service_role key |

Set on **Vercel**:

| Variable | Where to find it |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Same Project URL as above |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | Supabase dashboard → Settings → API → publishable/anon key |

Push `schema.sql` to the Supabase project the same way you would any Supabase migration (`supabase db push`, or run it directly against the project's connection string).

## Portal — Vercel

- Project root: `portal/` (Vercel auto-detects Next.js once the root is set).
- `next.config.ts` rewrites `/api/:path*` to `BACKEND_URL`, and `portal/proxy.ts` runs Supabase's session-refresh logic on every matched route — no extra Vercel config needed beyond env vars.

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | Supabase publishable key |
| `BACKEND_URL` | The Render backend's URL, e.g. `https://<your-app>.onrender.com` — used server-side, in `next.config.ts`'s rewrites and `app/auth/check/route.ts` |
| `NEXT_PUBLIC_BACKEND_URL` | Same URL, but the client-side copy — `login-form.tsx` and `site-header.tsx` read this in the browser (only `NEXT_PUBLIC_*` vars reach client code in Next.js, so both this and `BACKEND_URL` above need to be set to the same value) |

## Backend — Render

Web service, not a background worker (it serves HTTP + WebSocket traffic for `/chat`).

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `npm start` — already defined in `package.json` as `./.venv/bin/uvicorn backend.server:app --host 0.0.0.0 --port ${PORT:-8000}`, which picks up Render's injected `$PORT` automatically. Note that it invokes `./.venv/bin/uvicorn` specifically — make sure whatever build command you use actually creates `.venv` (e.g. `python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`), or point the start command at a plain `uvicorn` install instead. Since the start command is `npm start`, the service needs both a Python and a Node runtime available, not just Python.

| Variable | Notes |
|---|---|
| `ENVIRONMENT` | Set to `production` — this is also what selects the B2 storage client over `LocalStorageClient` (see `chainlit_app/app.py`'s `get_data_layer()`) |
| `PYTHON_VERSION`, `NODE_VERSION` | Pin Render's runtime versions, e.g. `3.12.10` and `26.8.2` — matching the versions in [INSTALL.md](./INSTALL.md#verified-versions). Node is required here too, since the start command runs through `npm start`. |
| `CORS_ORIGINS` | JSON array string, must include the portal's Vercel URL, e.g. `["https://<your-app>.vercel.app"]`. **The first entry is the one every backend→portal redirect uses** (`portal_url()` in `server.py` — login-required redirects, `/auth/bridge`, `/auth/logout-callback`), so if you list more than one origin (e.g. a preview deployment), put the canonical production URL first. |
| `CHAINLIT_AUTH_SECRET` | `chainlit create-secret` |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | See [Auth](#auth--supabase) |
| `DATABASE_URL` | See [Database](#database-local-or-neon) |
| `B2_BUCKET`, `B2_ENDPOINT`, `B2_KEY_ID`, `B2_APP_KEY`, `B2_REGION` | See [Storage](#storage--backblaze-b2) — **not currently listed in `.env.example`**, but required whenever `ENVIRONMENT=production` |
| `MINERU_TOKEN` + friends | See [MinerU](#mineru) |
| `OPIK_API_KEY`, `OPIK_WORKSPACE`, `OPIK_PROJECT_NAME` | See [Opik](#opik) |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | **Different from `MODAL_API_KEY` below.** These authenticate the Modal *Python SDK itself* to Modal's platform — required because `backend/ingest.py` and `backend/retrieve.py` call `modal.Cls.from_name("glyphscholar", ...)` directly from the running backend, not over plain HTTPS. Locally, `modal setup` writes these into `~/.modal.toml`; on Render, copy the `token_id` / `token_secret` values from that file into these two env vars instead. |
| `MODAL_API_KEY`, `HF_TOKEN`, `USE_MODAL_EMBED=True`, `USE_MODAL_RERANK=True`, `USE_MODAL_ANSWER=True` | `MODAL_API_KEY` is a separate, app-level shared secret (see [Modal](#modal)) that the deployed Modal classes check against — unrelated to the SDK auth above. Render has no GPU, so production should route inference through Modal rather than Ollama. |
| everything else in `.env.example` | Same meaning as local dev |

## Database — local or Neon

`DATABASE_URL` works identically whether it points at a local Postgres or a hosted Neon database — both `backend/db.py`'s connection pool and `chainlit_app/app.py`'s `get_data_layer()` just read the same variable. Use whichever fits the environment:

- **Local dev:** `postgresql://glyphuser:glyphpass@localhost:5432/glyphscholar` (per [INSTALL.md](./INSTALL.md)).
- **Production (Neon):** the pooled connection string from the Neon dashboard.

**Neon-specific gotcha:** Neon connection strings typically include `?sslmode=require&channel_binding=require`. `get_data_layer()` already strips `channel_binding` and translates `sslmode` → `ssl` before handing the URL to asyncpg — but `backend/db.py`'s own `asyncpg.create_pool(DATABASE_URL, ...)` does **not** do that translation. asyncpg rejects the `channel_binding` query parameter outright. So the `DATABASE_URL` env var itself needs `channel_binding` removed before it reaches `db.py` — e.g.:

```
DATABASE_URL="postgresql://user:pass@ep-xxxx.neon.tech/glyphscholar?sslmode=require"
```

Run the same migrations against Neon as you would locally:

```bash
psql "$DATABASE_URL" -f backend/migrations/001_app_schema.sql
psql "$DATABASE_URL" -f backend/migrations/002_chainlit_datalayer.sql
```

(`000_bootstrap.sql` creates the `glyphuser` role and database, which Neon's dashboard already does for you — skip it there.)

### Two databases, not one

It's easy to conflate these since Supabase is itself Postgres, but production actually runs **two separate Postgres databases**:

1. **Supabase's Postgres** — `auth.users`, `profiles`, `get_email_from_username` (`schema.sql`). Only ever touched via `SUPABASE_URL` (JWKS verification) and the `service_role` key (the resolve-login RPC).
2. **The app's own Postgres** (`DATABASE_URL`, local or Neon) — `document_chunks`, `app_users`, `chat_sessions`, plus Chainlit's own data-layer tables (`backend/migrations/001_app_schema.sql`, `002_chainlit_datalayer.sql`).

Nothing in the app ever queries Supabase's Postgres directly for chat/document data, and nothing queries the app's own Postgres for auth.

## Storage — Backblaze B2

`chainlit_app/app.py`'s `get_data_layer()` swaps `LocalStorageClient` (local dev) for Chainlit's built-in `S3StorageClient` (production) — B2's S3-compatible API works as a drop-in, so no custom client was needed:

```python
storage_provider = S3StorageClient(
    bucket=os.environ["B2_BUCKET"],
    endpoint_url=os.environ["B2_ENDPOINT"],
    aws_access_key_id=os.environ["B2_KEY_ID"],
    aws_secret_access_key=os.environ["B2_APP_KEY"],
    region_name=os.environ["B2_REGION"],
)
```

| Variable | Value |
|---|---|
| `B2_BUCKET` | Bucket name (e.g. `data_layer`) |
| `B2_ENDPOINT` | B2's S3-compatible endpoint for the bucket's region, e.g. `https://s3.us-west-004.backblazeb2.com` |
| `B2_KEY_ID` | Application key ID (Backblaze dashboard → App Keys) |
| `B2_APP_KEY` | Application key secret |
| `B2_REGION` | Region portion of the endpoint, e.g. `us-west-004` |

This only stores what Chainlit's data layer uploads (chat images/attachments via `create_element`) — it's unrelated to `UPLOAD_ROOT`, which is the app's own document-upload directory and stays local-disk regardless of environment (see `MAX_USER_UPLOAD_BYTES` / `UPLOAD_ROOT` in `.env.example`). Render's filesystem is ephemeral between deploys, so anything under `UPLOAD_ROOT` in production doesn't survive a redeploy — that's fine for in-flight ingestion, but not a place to expect long-term persistence.

## MinerU

Hosted API, nothing to deploy — same `MINERU_*` variables as local dev (see [INSTALL.md § MinerU](./INSTALL.md#4-mineru)), set on Render instead of `.env`.

## Opik

Hosted (comet.com), nothing to deploy — same `OPIK_API_KEY`, `OPIK_WORKSPACE`, `OPIK_PROJECT_NAME` as local dev, set on Render.

## Modal

Deployed independently of Render/Vercel — Modal's own serverless GPU containers run the embedding, reranker, and answer models. There are two unrelated sets of Modal credentials in play:

- **`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`** — authenticate the Modal SDK itself to Modal's platform. Needed anywhere the SDK runs: your own machine (via `modal setup`, which writes them to `~/.modal.toml`) *and* the Render backend, since `backend/ingest.py`/`backend/retrieve.py` call `modal.Cls.from_name(...)` directly at request time.
- **`MODAL_API_KEY`** — an app-level shared secret with no relation to the above. It's pushed into a Modal Secret that the deployed `EmbeddingServer`/`RerankServer`/`AnswerServer` classes check against, so the same value must also be set as `MODAL_API_KEY` on Render.

From a machine with the Modal CLI authenticated (`modal setup`):

```bash
modal secret create --force glyphscholar-secret \
  MODAL_API_KEY="<value also set as MODAL_API_KEY on Render>" \
  HF_TOKEN="<value also set as HF_TOKEN on Render>"

modal run modal/services.py::download_models   # one-time, ~10 GB into a Modal Volume
modal deploy modal/services.py
```

Set `USE_MODAL_EMBED=True`, `USE_MODAL_RERANK=True`, `USE_MODAL_ANSWER=True` on Render so the backend routes inference through Modal instead of Ollama (Render's web service has no GPU). Redeploying `modal/services.py` doesn't require redeploying the Render backend, and vice versa — they're independent.

## Not hosted anywhere in production

**Ollama** is a local-only fallback for development (`USE_MODAL_*=False`). It isn't deployed to Render or anywhere else — production should run with the `USE_MODAL_*` flags on, per [Modal](#modal) above.