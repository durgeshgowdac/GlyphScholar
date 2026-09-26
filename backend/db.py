import asyncio
import json
import os
import shutil

import asyncpg

from backend.storage import UPLOAD_ROOT, safe_identifier, safe_session_id

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost:5432/glyphscholar")

# Chainlit's SQLAlchemyDataLayer (chainlit_app/app.py) opens its own,
# separate connection pool to the same database for threads/steps/
# elements/feedbacks. This pool and that one are sized independently —
# check the installed chainlit version's SQLAlchemyDataLayer signature
# for its own pool_size/max_overflow knobs and tune together, especially
# before raising expected concurrent users.
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "5"))

pool_instance: asyncpg.Pool | None = None
pool_create_lock: asyncio.Lock | None = None


async def get_pool() -> asyncpg.Pool:
    global pool_instance, pool_create_lock
    if pool_instance is not None:
        return pool_instance
    if pool_create_lock is None:
        pool_create_lock = asyncio.Lock()
    async with pool_create_lock:
        if pool_instance is None:
            pool_instance = await asyncpg.create_pool(
                DATABASE_URL, min_size=DB_POOL_MIN_SIZE, max_size=DB_POOL_MAX_SIZE, init=init_connection,
            )
    return pool_instance


async def init_connection(conn: asyncpg.Connection) -> None:
    """
    Registers a pool-wide codec for JSONB columns.

    This allows SELECT/INSERT of JSONB columns to use Python dictionaries directly,
    avoiding per-call json.loads/dumps overhead.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def close_pool() -> None:
    global pool_instance
    if pool_instance is not None:
        await pool_instance.close()
        pool_instance = None


async def init_db() -> None:
    """
    Checks database connectivity.

    Schema management is handled by backend/migrations/*.sql.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")


async def upsert_chainlit_user(identifier: str, metadata: dict) -> str:
    """
    Upserts user row for Chainlit's SQLAlchemyDataLayer.

    Necessary because Chainlit's auto-provisioning is unreliable with some header-auth
    configurations. Explicitly upserting in header_auth_callback ensures the row exists.

    ID is generated on first insert; re-logins refresh metadata.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (id, identifier, "createdAt", metadata)
            VALUES (gen_random_uuid(), $1, now()::text, $2::jsonb) ON CONFLICT (identifier) DO
            UPDATE
                SET metadata = EXCLUDED.metadata
                RETURNING id
            """,
            identifier, metadata or {},
        )
        return str(row["id"])


async def upsert_user(user_id: str, email: str, username: str, role: str) -> None:
    """
    Mirrors the authenticated Supabase user into the local `app_users` table.

    Called on every chat start; idempotent and updates last_seen.

    NOTE: This is `app_users`, not the `users` table used by Chainlit's
    SQLAlchemyDataLayer.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO app_users (id, email, username, role, last_seen)
            VALUES ($1, $2, $3, $4, NOW()) ON CONFLICT (id) DO
            UPDATE
                SET email = EXCLUDED.email,
                username = EXCLUDED.username,
                role = EXCLUDED.role,
                last_seen = NOW()
            """,
            user_id, email, username or None, role,
        )


async def create_session(
        user_id: str,
        session_id: str | None = None,
        identifier: str | None = None,
        title: str | None = None,
) -> str:
    """
    Creates a chat_sessions row. Pass `session_id` explicitly to make this
    row share its primary key with a Chainlit thread id (recommended, once
    the SQLAlchemy data layer is wired in — see chainlit_app/app.py) so that
    uploads, documents, and the resumable chat thread all key off the same
    id. If omitted, Postgres generates one via gen_random_uuid().
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if session_id:
            row = await conn.fetchrow(
                """
                INSERT INTO chat_sessions (id, user_id, identifier, title)
                VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO
                UPDATE
                    SET identifier = EXCLUDED.identifier
                    RETURNING id
                """,
                session_id, user_id, identifier, title,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO chat_sessions (user_id, identifier, title)
                VALUES ($1, $2, $3) RETURNING id
                """,
                user_id, identifier, title,
            )
        return str(row["id"])


async def get_session_full_text(session_id: str) -> list[dict]:
    """All indexed text chunks for a session, in document order — the raw
    material for full-context stuffing (see on_message's full-text check)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT document_id, chunk_index, content, metadata
               FROM document_chunks
               WHERE session_id = $1
                 AND chunk_type = 'text'
               ORDER BY document_id, chunk_index""",
            session_id,
        )
        return [dict(r) for r in rows]


async def get_user_sessions(user_id: str) -> list[dict]:
    """Useful for a session history sidebar later (or debugging)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, identifier, title, created_at
               FROM chat_sessions
               WHERE user_id = $1
               ORDER BY updated_at DESC LIMIT 20""",
            user_id,
        )
        return [dict(r) for r in rows]


async def save_document(
        user_id: str,
        session_id: str,
        filename: str,
        file_type: str,
        file_path: str,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        metadata: dict | None = None,
) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO documents
            (user_id, session_id, filename, file_type, file_path,
             mime_type, size_bytes, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id
            """,
            user_id, session_id, filename, file_type, file_path,
            mime_type, size_bytes, metadata or {},
        )
        return str(row["id"])


async def delete_old_sessions(older_than_days: int = 30) -> int:
    """Delete upload directories, document/chunk rows, and chat_sessions rows
    for sessions untouched for `older_than_days` days. Returns the number of
    sessions deleted. Called from a periodic background task or a
    maintenance CLI command.

    Mirrors GlyphScholarDataLayer.delete_thread's cleanup (chainlit_app/app.py)
    instead of only removing chat_sessions: documents.session_id is
    ON DELETE SET NULL (see backend/migrations/001_app_schema.sql), so leaving
    `documents` untouched would orphan both the DB rows and the files/images
    on disk, and would keep counting against the user's upload quota
    (get_user_upload_size_bytes sums by user_id, not by live session)
    forever, with no way for the user to reclaim it short of manually
    deleting every thread that ever touched those documents.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Fetch file paths and identifiers, then bulk-delete in one transaction
            stale_sessions = await conn.fetch(
                """
                SELECT id, identifier
                FROM chat_sessions
                WHERE updated_at < NOW() - make_interval(days = > $1)
                """,
                older_than_days,
            )
            if not stale_sessions:
                return 0

            stale_ids = [row["id"] for row in stale_sessions]

            # rag_retrievals.step_id is deliberately unenforced (no FK —
            # see 001_app_schema.sql) since steps is Chainlit-owned, so it
            # won't cascade on its own. Purge rows for any step belonging
            # to a thread we're about to delete before Chainlit's own
            # cascade removes the steps themselves.
            step_rows = await conn.fetch(
                'SELECT id FROM steps WHERE "threadId" = ANY($1::uuid[])',
                stale_ids,
            )
            step_ids = [r["id"] for r in step_rows]
            if step_ids:
                await conn.execute(
                    "DELETE FROM rag_retrievals WHERE step_id = ANY($1::uuid[])",
                    step_ids,
                )

            # Collect file paths before deleting rows
            doc_rows = await conn.fetch(
                "SELECT file_path FROM documents WHERE session_id = ANY($1::uuid[])",
                stale_ids,
            )

            # Bulk deletes — documents cascade to document_chunks
            await conn.execute(
                "DELETE FROM documents WHERE session_id = ANY($1::uuid[])", stale_ids
            )
            # `chat_sessions.id` is reused as the Chainlit thread id (see
            # create_session's docstring) — delete the matching `threads`
            # rows too so this periodic cleanup doesn't leave Chainlit
            # "ghost" threads (with steps/elements/feedbacks still
            # cascading off them) pointing at app-side rows we just
            # removed. Mirrors what GlyphScholarDataLayer.delete_thread
            # does for the interactive delete path.
            await conn.execute(
                "DELETE FROM threads WHERE id = ANY($1::uuid[])", stale_ids
            )
            await conn.execute(
                "DELETE FROM chat_sessions WHERE id = ANY($1::uuid[])", stale_ids
            )

        # Filesystem cleanup outside the transaction
        for doc_row in doc_rows:
            file_path = doc_row.get("file_path")
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError as e:
                    # Log as error for monitoring
                    print(f"[delete_old_sessions] ERROR: failed to remove {file_path}: {e}")

        for row in stale_sessions:
            session_dir = (UPLOAD_ROOT / safe_identifier(row["identifier"] or "anonymous") / safe_session_id(
                str(row["id"])))
            shutil.rmtree(session_dir, ignore_errors=True)

        return len(stale_ids)


async def add_document_size_bytes(document_id: str, extra_bytes: int) -> None:
    """Add rendered-page-image bytes (PixelRAG PNGs under
    <upload_dir>/images/<upload_uuid>/, written by
    backend.ingest.render_pdf_pages) on top of the original upload's
    size_bytes. Without this, get_user_upload_size_bytes() only counts the
    original file and the quota check in chainlit_app/app.py silently
    undercounts real disk usage — sometimes by many times over for
    image-heavy PDFs."""
    if extra_bytes <= 0:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET size_bytes = COALESCE(size_bytes, 0) + $1 WHERE id = $2",
            extra_bytes, document_id,
        )


async def get_user_upload_size_bytes(user_id: str) -> int:
    """Sum of all upload sizes for a user across all sessions."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM documents WHERE user_id = $1",
            user_id,
        )
        return int(result)


async def session_has_chunks(session_id: str) -> bool:
    """Cheap existence check so on_message can skip the embed+rerank
    round trip entirely for sessions with no indexed documents yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM document_chunks WHERE session_id = $1)",
            session_id,
        ))


async def get_session_pdfs(session_id: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, filename, file_path
               FROM documents
               WHERE session_id = $1
                 AND file_type = 'pdf'
               ORDER BY created_at""",
            session_id,
        )
        return [dict(r) for r in rows]
