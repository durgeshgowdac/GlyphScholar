# Contributing to GlyphScholar

Thanks for your interest in improving GlyphScholar. This document covers how to propose changes, report issues, and get a development environment running.

By participating in this project, you agree to abide by our [Code of Conduct](./CODE_OF_CONDUCT.md).

## Table of Contents

- [Ways to Contribute](#ways-to-contribute)
- [Development Setup](#development-setup)
- [Project Layout](#project-layout)
- [Coding Guidelines](#coding-guidelines)
- [Commit Messages](#commit-messages)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [Reporting Bugs](#reporting-bugs)
- [Suggesting Features](#suggesting-features)
- [Questions](#questions)

## Ways to Contribute

- **Bug fixes** — corrections to existing behavior in the backend, Chainlit app, Modal services, or the portal frontend.
- **Features** — new retrieval strategies, ingestion improvements, UI additions, and so on.
- **Documentation** — improvements to `README.md`, `INSTALL.md`, or inline code comments.
- **Issue triage** — reproducing reported bugs, confirming fixes, and reviewing open pull requests.

If you're planning a larger change (a new architecture piece, a breaking API change, a new external service dependency), please open an issue first to discuss the approach before investing significant time.

## Development Setup

Follow [INSTALL.md](./INSTALL.md) to get PostgreSQL, Ollama, Supabase, Modal, MinerU, and Chainlit configured locally. In short:

```bash
git clone https://github.com/<your-org>/glyphscholar.git
cd glyphscholar
cp .env.example .env

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

npm install --prefix portal
npm install

npm run dev
```

## Project Layout

| Path | Contents |
|---|---|
| `backend/` | FastAPI app, ingestion pipeline, retrieval logic, SQL migrations |
| `chainlit_app/` | Chainlit chat UI |
| `modal/` | Serverless GPU inference services |
| `portal/` | Next.js frontend (auth pages, landing page) |
| `supabase/` | Supabase project config and migrations |

See the [Project Structure](./INSTALL.md#project-structure) section of `INSTALL.md` for the full breakdown.

## Coding Guidelines

- **Python** — follow [PEP 8](https://peps.python.org/pep-0008/). Prefer explicit, typed function signatures where practical (the codebase already uses `typing` throughout `backend/`). Keep async database access going through `backend/db.py` rather than opening new connections elsewhere.
- **TypeScript / Next.js (`portal/`)** — match the existing formatting enforced by `eslint.config.mjs`. Keep Supabase client logic inside `lib/supabase/`.
- **SQL migrations** — add new migrations as a new numbered file under `backend/migrations/` (e.g. `003_your_change.sql`); don't edit an existing, already-applied migration.
- Keep pull requests focused — one logical change per PR is easier to review than several unrelated changes bundled together.

## Commit Messages

Write clear, imperative commit messages that describe *what* the change does, e.g.:

```
Fix pixel retrieval fallback distance calculation
Add MinerU formula-parsing toggle to ingestion config
Update INSTALL.md with pgvector setup step
```

## Submitting a Pull Request

1. Fork the repository and create a branch from `main`:
   ```bash
   git checkout -b your-name/short-description
   ```
2. Make your changes, and verify the app still runs locally per [INSTALL.md](./INSTALL.md).
3. Push your branch and open a pull request using the [pull request template](./.github/PULL_REQUEST_TEMPLATE.md).
4. Fill in the description, link any related issues, and note any manual testing you performed — there's no automated test suite yet, so a clear description of what you checked helps reviewers a lot.
5. Be responsive to review feedback. A maintainer will merge once the PR is approved.

## Reporting Bugs

Please use the [bug report template](./.github/ISSUE_TEMPLATE/bug_report.md) when opening an issue. Include:

- Steps to reproduce
- Expected vs. actual behavior
- Relevant logs (backend, Chainlit, or `modal app logs glyphscholar` output)
- Whether you're running inference via Ollama or Modal

## Suggesting Features

Please use the [feature request template](./.github/ISSUE_TEMPLATE/feature_request.md). Describe the problem you're trying to solve, not just the solution — it makes it easier to find the best approach together.

## Questions

If something in this guide or in `INSTALL.md` doesn't work as described, please open an issue — it likely means the docs need updating.