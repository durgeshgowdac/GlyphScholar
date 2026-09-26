-- App-owned schema: everything the RAG/chat backend reads and writes itself.
-- Chainlit's own tables (users/threads/steps/elements/feedbacks) live in
-- 002_chainlit_datalayer.sql, not here — see that file for why they're kept
-- separate. Safe to re-run: every statement is IF NOT EXISTS / idempotent.

-- ─── Extensions ───────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto"; -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "vector";
-- pgvector for RAG

-- ─── App users (mirror of Supabase auth users, for local FK reference) ────
-- Named app_users, not users — Chainlit's SQLAlchemyDataLayer (see
-- 002_chainlit_datalayer.sql) hardcodes a table literally called `users` in
-- its own raw SQL and that name is not configurable, so this table can't be
-- named `users` without colliding with it. documents/chat_sessions FK
-- against app_users; Chainlit's `users` table is separate and Chainlit
-- manages it itself.
CREATE TABLE IF NOT EXISTS app_users
(
    id         TEXT PRIMARY KEY, -- matches Supabase user_id
    email      TEXT,
    username   TEXT,
    role       TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen  TIMESTAMPTZ DEFAULT NOW()
);

-- Enforced format so a username can never collide with another once
-- sanitized into a filesystem path component (see backend/storage.py).
DO
$$
    BEGIN
        ALTER TABLE app_users
            ADD CONSTRAINT app_users_username_format
                CHECK (username IS NULL OR username ~ '^[a-z0-9_]+$');
    EXCEPTION
        WHEN duplicate_object THEN NULL;
    END
$$;

-- ─── Chat sessions ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_sessions
(
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT NOT NULL REFERENCES app_users (id) ON DELETE CASCADE,
    identifier TEXT, -- human-readable grouping key
    title      TEXT, -- optional: auto-generated from first message
    created_at TIMESTAMPTZ      DEFAULT NOW(),
    updated_at TIMESTAMPTZ      DEFAULT NOW()
);

-- ─── Chat messages ──────────────────────────────────────────────────────
-- Not written to once Chainlit's SQLAlchemyDataLayer is wired in (see
-- chainlit_app/app.py) — Chainlit's own `steps` table is the real message log
-- that powers the resumable-thread sidebar. Kept only because rag_retrievals
-- below still exists as a lightweight audit trail alongside it; drop this
-- table if you don't need a second copy for anything else.
CREATE TABLE IF NOT EXISTS chat_messages
(
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content     TEXT NOT NULL,
    token_count INT,
    created_at  TIMESTAMPTZ      DEFAULT NOW()
);

-- ─── Documents (for RAG) ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents
(
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT NOT NULL REFERENCES app_users (id) ON DELETE CASCADE,
    session_id UUID REFERENCES chat_sessions (id) ON DELETE SET NULL,
    filename   TEXT NOT NULL,
    file_type  TEXT, -- 'pdf', 'image', 'text' etc.
    file_path  TEXT, -- local storage path
    mime_type  TEXT,
    size_bytes BIGINT,
    metadata   JSONB            DEFAULT '{}',
    created_at TIMESTAMPTZ      DEFAULT NOW()
);

-- ─── Document chunks (for RAG + PixelRAG) ──────────────────────────────────
-- Two embedding backends are supported side by side, each with its own pair
-- of columns, rather than one column shared across backends:
--   *_modal  halfvec(2048) — Modal-hosted Qwen3-VL-Embedding-2B (EMBED_DIM=2048)
--   *_local  halfvec(768)  — Ollama's nomic-embed-text
-- pgvector's halfvec column type is fixed-dimension, so a single column
-- can't hold both without truncating or padding one of them — hence the
-- split into _modal/_local pairs instead of one text_embedding column.
-- ingest.py picks which pair to populate based on EMBED_MODEL; retrieve.py
-- must query against the matching pair for whichever model produced the
-- query embedding. If you add another embedding backend at a new
-- dimension, add another column pair (and HNSW index pair) rather than
-- reusing one of these.
CREATE TABLE IF NOT EXISTS document_chunks
(
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id           UUID NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    session_id            UUID REFERENCES chat_sessions (id) ON DELETE CASCADE,
    chunk_index           INT  NOT NULL,
    content               TEXT,                          -- raw text (null for image chunks)
    chunk_type            TEXT             DEFAULT 'text'
        CHECK (chunk_type IN ('text', 'image', 'table')),
    image_path            TEXT,                          -- for PixelRAG image chunks
    text_embedding_modal  halfvec(2048),                 -- Modal Qwen3-VL-Embedding-2B
    text_embedding_local  halfvec(768),                  -- Ollama nomic-embed-text
    image_embedding_modal halfvec(2048),                 -- Modal Qwen3-VL-Embedding-2B
    image_embedding_local halfvec(768),                  -- Ollama nomic-embed-text
    sparse_embedding      JSONB,                         -- hybrid RAG sparse vectors (BM25 etc.)
    metadata              JSONB            DEFAULT '{}', -- page number, bounding box, etc.
    created_at            TIMESTAMPTZ      DEFAULT NOW()
);

-- ─── RAG retrievals (audit trail of what was retrieved per turn) ──────────
-- step_id is an unenforced reference to steps.id (Chainlit-managed, see
-- 002_chainlit_datalayer.sql) — no FK, since steps is owned by
-- SQLAlchemyDataLayer and must not be formally coupled to app schema.
CREATE TABLE IF NOT EXISTS rag_retrievals
(
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    step_id        UUID NOT NULL,
    chunk_id       UUID NOT NULL REFERENCES document_chunks (id) ON DELETE CASCADE,
    score          FLOAT,
    retrieval_type TEXT
        CHECK (retrieval_type IN ('dense', 'sparse', 'hybrid', 'pixel', 'rerank', 'raster_fallback')),
    created_at     TIMESTAMPTZ      DEFAULT NOW()
);

COMMENT ON COLUMN rag_retrievals.step_id IS
    'Unenforced reference to steps.id (Chainlit-managed). No FK because '
        'steps is owned by SQLAlchemyDataLayer and must not be coupled to app schema.';

-- ─── Indexes ────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON chat_messages (session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON chat_sessions (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_documents_session
    ON documents (session_id);

CREATE INDEX IF NOT EXISTS idx_documents_user
    ON documents (user_id);

CREATE INDEX IF NOT EXISTS idx_chunks_document
    ON document_chunks (document_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_chunks_session
    ON document_chunks (session_id);

-- General session+type lookups (both text and image rows — no filter, so
-- neither chunk_type is structurally excluded from the index).
-- DROP+CREATE (not just IF NOT EXISTS) because this replaces an earlier
-- version of this same index that had a `WHERE text_embedding IS NOT NULL`
-- filter — IF NOT EXISTS alone would leave that stale definition in place
-- for anyone who already ran this migration once.
DROP INDEX IF EXISTS idx_chunks_session_type;
CREATE INDEX idx_chunks_session_type
    ON document_chunks (session_id, chunk_type);

-- Specifically supports retrieve.image_retrieve()'s
-- WHERE chunk_type = 'image' AND image_path IS NOT NULL filter.
CREATE INDEX IF NOT EXISTS idx_chunks_session_image
    ON document_chunks (session_id)
    WHERE chunk_type IN ('image', 'table') AND image_path IS NOT NULL;

-- Supports `metadata ->> 'page' = ...` equality lookups in
-- retrieve.image_retrieve()'s page-colocation fallback (a GIN index on
-- the whole jsonb column can't accelerate ->> text-extraction equality).
CREATE INDEX IF NOT EXISTS idx_chunks_metadata_page
    ON document_chunks ((metadata ->> 'page'));

-- HNSW ANN indexes, one pair per embedding backend (see the column
-- comment on document_chunks above for why the columns are split this
-- way). This provides better recall than ivfflat.
CREATE INDEX IF NOT EXISTS idx_chunks_text_embedding_modal
    ON document_chunks USING hnsw (text_embedding_modal halfvec_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_chunks_text_embedding_local
    ON document_chunks USING hnsw (text_embedding_local halfvec_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_chunks_image_embedding_modal
    ON document_chunks USING hnsw (image_embedding_modal halfvec_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_chunks_image_embedding_local
    ON document_chunks USING hnsw (image_embedding_local halfvec_cosine_ops);

-- ─── Auto-update updated_at on chat_sessions ──────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
    RETURNS TRIGGER AS
$$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sessions_updated_at ON chat_sessions;
CREATE TRIGGER trg_sessions_updated_at
    BEFORE UPDATE
    ON chat_sessions
    FOR EACH ROW
EXECUTE FUNCTION update_updated_at();