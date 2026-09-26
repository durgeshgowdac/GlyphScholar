"""Log RAG retrievals to rag_retrievals table for audit/eval."""
import uuid

from backend.db import get_pool


def is_real_chunk(chunk: dict) -> bool:
    """rag_retrievals.chunk_id is a UUID FK into document_chunks. Most
    chunks are real rows there, but retrieve.raster_fallback_for_query()
    synthesizes ephemeral image chunks on the fly (id like
    "raster-fallback-<doc_id>-<page>", metadata["query_time"] = True) that
    are deliberately never written to document_chunks — rendered, embedded,
    and handed to rerank() for this one query only. Logging those would
    fail: their id isn't a UUID at all, and even a well-formed UUID would
    still violate the FK since no matching document_chunks row exists.
    Skip anything that doesn't look like a real, persisted chunk row."""
    if chunk.get("metadata", {}).get("query_time"):
        return False
    try:
        uuid.UUID(str(chunk.get("id")))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


async def log_retrievals(
        step_id: str,
        text_chunks: list[dict],
        image_chunks: list[dict],
) -> None:
    all_chunks = [c for c in text_chunks + image_chunks if is_real_chunk(c)]

    if not all_chunks:
        return

    records = [
        (
            step_id,
            chunk["id"],
            # Use `is not None` chained fallbacks rather than `or` — a
            # legitimate rerank_score/hybrid_score of exactly 0.0 is falsy
            # and would otherwise be silently discarded in favor of the
            # next fallback (or the final 0), misattributing the score.
            float(
                chunk["rerank_score"] if chunk.get("rerank_score") is not None
                else chunk["hybrid_score"] if chunk.get("hybrid_score") is not None
                else 0
            ),
            chunk.get("retrieval_type", "dense"),
        )
        for chunk in all_chunks
    ]

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO rag_retrievals (step_id, chunk_id, score, retrieval_type)
            VALUES ($1, $2, $3, $4)
            """,
            records
        )
