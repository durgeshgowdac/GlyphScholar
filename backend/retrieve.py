"""
Hybrid retrieval for GlyphScholar: dense (pgvector) + sparse (BM25 re-rank).
Returns ranked list of chunk dicts to inject into the prompt.

PixelRAG's visual index is primarily MinerU's own figure/table/equation
crops (see backend/ingest.py's crops_from_content_list) — PIXEL_TOP_K below
is "top-K crops", not "top-K rasterized pages". Full-page rasterization
still exists, but only as a fallback: at ingest time for pages MinerU's
layout detector didn't classify into any block at all (backend/ingest.py's
render_pdf_pages), and at query time (raster_fallback_for_query below) when
both the text chunks and the crop chunks score low/ambiguous for a page.
"""

import asyncio
import base64
import os
from pathlib import Path

import httpx

from backend.db import get_pool

MODAL_API_KEY = os.getenv("MODAL_API_KEY", "")

USE_MODAL_EMBED = os.getenv("USE_MODAL_EMBED", "True").lower() in ("true", "1", "yes")
# Modal embed always forces Modal rerank on, regardless of USE_MODAL_RERANK —
# the local/BM25 reranker doesn't score well against Qwen3-VL embedding
# space. Local embed defaults to local rerank but can still opt into Modal
# rerank via USE_MODAL_RERANK.
USE_MODAL_RERANK = USE_MODAL_EMBED or (
        os.getenv("USE_MODAL_RERANK", "False").lower() in ("true", "1", "yes")
)
EMBED_COL_SUFFIX = "modal" if USE_MODAL_EMBED else "local"
TEXT_EMBED_COL = f"text_embedding_{EMBED_COL_SUFFIX}"
IMAGE_EMBED_COL = f"image_embedding_{EMBED_COL_SUFFIX}"

RERANK_MODEL = os.getenv("RERANK_MODEL", "Qwen/Qwen3-VL-Reranker-2B")
RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", "5"))

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "Qwen/Qwen3-VL-Embedding-2B")  # Modal (HF repo id)
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")  # Ollama (local pull tag)

DENSE_TOP_K = int(os.getenv("DENSE_TOP_K", "20"))  # candidates from pgvector
FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "5"))  # chunks injected into prompt
PIXEL_TOP_K = int(os.getenv("PIXEL_TOP_K", "2"))  # crops (or fallback page rasters) for PixelRAG

# cosine distance thresholds (0 = identical) below which a hit is trusted;
# at/above these, retrieve() treats the corresponding side as "didn't
# really find anything" for the ambiguous-page raster fallback below.
TEXT_FALLBACK_DISTANCE = float(os.getenv("TEXT_FALLBACK_DISTANCE", "0.35"))
PIXEL_FALLBACK_DISTANCE = float(os.getenv("PIXEL_FALLBACK_DISTANCE", "0.35"))


async def embed_query(query: str, http: httpx.AsyncClient) -> list[float]:
    is_ollama = not USE_MODAL_EMBED

    if is_ollama:
        resp = await http.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": OLLAMA_EMBED_MODEL, "input": [query]},
            timeout=30.0,
        )
        raise_for_ollama_status(resp, OLLAMA_EMBED_MODEL, OLLAMA_BASE_URL)
        return resp.json()["embeddings"][0]
    else:
        # Modal's EmbeddingServer.embeddings (modal/embed_rerank.py) is a
        # @modal.fastapi_endpoint — each one gets its own dedicated
        # hostname with NO sub-path route. POST directly to EMBED_API_URL
        # itself; appending "/v1/embeddings" 404s, since that path doesn't
        # exist on a single-function Modal web endpoint the way it would
        # on a generic multi-route OpenAI-compatible server.
        import modal
        EmbeddingServer = modal.Cls.from_name("glyphscholar", "EmbeddingServer")
        resp = await EmbeddingServer().embeddings.remote.aio(
            {"model": EMBED_MODEL, "input": [query], "api_key": MODAL_API_KEY}
        )
        return resp["data"][0]["embedding"]


def raise_for_ollama_status(resp: httpx.Response, model: str, base_url: str) -> None:
    """Mirrors backend.ingest.raise_for_ollama_status — a bare 404 from
    Ollama almost always means the model hasn't been pulled yet, not a
    server-side problem, so surface that instead of a raw
    httpx.HTTPStatusError."""
    if resp.status_code == 404:
        raise RuntimeError(
            f"Ollama returned 404 for {resp.request.url}. Most likely the "
            f"model '{model}' hasn't been pulled yet — run "
            f"`ollama pull {model}` and confirm it appears in "
            f"`ollama list`. Also double-check the embed URL "
            f"({base_url!r}) points at a running `ollama serve` with no "
            f"trailing /v1."
        )
    resp.raise_for_status()


async def dense_retrieve(
        query_embedding: list[float],
        session_id: str,
        document_ids: list[str] | None,
        top_k: int,
) -> list[dict]:
    """ANN search over text_embedding for a session (optionally filtered to specific docs)."""
    pool = await get_pool()
    # Format embedding as pgvector literal
    vec_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    async with pool.acquire() as conn:
        if document_ids:
            rows = await conn.fetch(
                f"""
                SELECT id,
                       document_id,
                       chunk_index,
                       content,
                       chunk_type,
                       image_path,
                       sparse_embedding,
                       metadata,
                       {TEXT_EMBED_COL} <=> $1::halfvec AS distance
                FROM document_chunks
                WHERE session_id = $2
                  AND document_id = ANY ($3::uuid[])
                  AND chunk_type = 'text'
                  AND {TEXT_EMBED_COL} IS NOT NULL
                ORDER BY distance
                    LIMIT $4
                """,
                vec_str, session_id, document_ids, top_k,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT id,
                       document_id,
                       chunk_index,
                       content,
                       chunk_type,
                       image_path,
                       sparse_embedding,
                       metadata,
                       {TEXT_EMBED_COL} <=> $1::halfvec AS distance
                FROM document_chunks
                WHERE session_id = $2
                  AND chunk_type = 'text'
                  AND {TEXT_EMBED_COL} IS NOT NULL
                ORDER BY distance
                    LIMIT $3
                """,
                vec_str, session_id, top_k,
            )
    return [dict(r) for r in rows]


async def dense_image_retrieve(
        query_embedding: list[float],
        session_id: str,
        document_ids: list[str] | None,
        top_k: int,
) -> list[dict]:
    """ANN search directly over image_embedding — real visual similarity,
    not the page-co-location heuristic below. Returns [] when no image
    chunks in this session have an embedding yet (e.g. Ollama-only setups
    that don't run a vision embedding model, or ingestion in progress)."""
    pool = await get_pool()
    vec_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    async with pool.acquire() as conn:
        if document_ids:
            rows = await conn.fetch(
                f"""
                SELECT id,
                       document_id,
                       content,
                       image_path,
                       metadata,
                       {IMAGE_EMBED_COL} <=> $1::halfvec AS distance
                FROM document_chunks
                WHERE session_id = $2
                  AND document_id = ANY ($3::uuid[])
                  AND chunk_type IN ('image', 'table')
                  AND image_path IS NOT NULL
                  AND {IMAGE_EMBED_COL} IS NOT NULL
                ORDER BY distance
                    LIMIT $4
                """,
                vec_str, session_id, document_ids, top_k,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT id,
                       document_id,
                       content,
                       image_path,
                       metadata,
                       {IMAGE_EMBED_COL} <=> $1::halfvec AS distance
                FROM document_chunks
                WHERE session_id = $2
                  AND chunk_type IN ('image', 'table')
                  AND image_path IS NOT NULL
                  AND {IMAGE_EMBED_COL} IS NOT NULL
                ORDER BY distance
                    LIMIT $3
                """,
                vec_str, session_id, top_k,
            )
    return [dict(r) for r in rows]


async def image_retrieve(
        query_embedding: list[float],
        session_id: str,
        document_ids: list[str] | None,
        top_k: int,
) -> list[dict]:
    # Prefer real visual similarity search when image embeddings exist.
    dense_hits = await dense_image_retrieve(query_embedding, session_id, document_ids, top_k)
    if dense_hits:
        return dense_hits

    # Fallback: no image embeddings indexed for this session (e.g. local
    # Ollama-only deployment) — approximate via pages near the best text
    # matches instead of returning nothing.
    pool = await get_pool()
    vec_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    async with pool.acquire() as conn:
        if document_ids:
            nearby_text = await conn.fetch(
                f"""
                SELECT metadata ->>'page' AS page, document_id
                FROM document_chunks
                WHERE session_id = $1
                  AND document_id = ANY ($2::uuid[])
                  AND chunk_type = 'text'
                  AND {TEXT_EMBED_COL} IS NOT NULL
                ORDER BY {TEXT_EMBED_COL} <=> $3::halfvec
                    LIMIT $4
                """,
                session_id, document_ids, vec_str, top_k * 3,
            )
        else:
            nearby_text = await conn.fetch(
                f"""
                SELECT metadata ->>'page' AS page, document_id
                FROM document_chunks
                WHERE session_id = $1
                  AND chunk_type = 'text'
                  AND {TEXT_EMBED_COL} IS NOT NULL
                ORDER BY {TEXT_EMBED_COL} <=> $2::halfvec
                    LIMIT $3
                """,
                session_id, vec_str, top_k * 3,
            )

        if not nearby_text:
            return []

        page_pairs = [(str(r["document_id"]), str(r["page"])) for r in nearby_text if r["page"] is not None]
        if not page_pairs:
            return []

        doc_id_list = [p[0] for p in page_pairs]
        page_num_list = [p[1] for p in page_pairs]

        image_rows = await conn.fetch(
            """
            SELECT id, document_id, content, image_path, metadata
            FROM document_chunks
            WHERE session_id = $1
              AND chunk_type IN ('image', 'table')
              AND image_path IS NOT NULL
              AND EXISTS (SELECT 1
                          FROM unnest($2::uuid[], $3::text[]) AS t(doc_id, page_num)
                          WHERE document_id = t.doc_id
                            AND metadata ->> 'page' = t.page_num)
                LIMIT $4
            """,
            session_id, doc_id_list, page_num_list, top_k,
        )

    if image_rows:
        return [dict(r) for r in image_rows]

    # Last-resort fallback: standalone image uploads (no PDF page context,
    # so no "page" in their metadata — the co-location join above can never
    # match them) that also have no embedding (non-Modal setups, so
    # dense_image_retrieve missed them too). Without this they're
    # accepted, stored, billed against quota, and then never surface in
    # any answer. Not query-relevance-ranked — just "this session has a
    # standalone image, so include it" — but that beats silent exclusion.
    return await standalone_image_fallback(session_id, document_ids, top_k)


async def standalone_image_fallback(
        session_id: str,
        document_ids: list[str] | None,
        top_k: int,
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if document_ids:
            rows = await conn.fetch(
                f"""
                SELECT id, document_id, content, image_path, metadata
                FROM document_chunks
                WHERE session_id = $1
                  AND document_id = ANY ($2::uuid[])
                  AND chunk_type IN ('image', 'table')
                  AND image_path IS NOT NULL
                  AND {IMAGE_EMBED_COL} IS NULL
                  AND metadata ->> 'standalone' = 'true'
                    LIMIT $3
                """,
                session_id, document_ids, top_k,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT id, document_id, content, image_path, metadata
                FROM document_chunks
                WHERE session_id = $1
                  AND chunk_type IN ('image', 'table')
                  AND image_path IS NOT NULL
                  AND {IMAGE_EMBED_COL} IS NULL
                  AND metadata ->> 'standalone' = 'true'
                    LIMIT $2
                """,
                session_id, top_k,
            )
    return [dict(r) for r in rows]


def bm25_rerank(query: str, candidates: list[dict]) -> list[dict]:
    query_terms = query.lower().split()

    # Compute raw BM25 scores first
    raw_scores = []
    for c in candidates:
        sparse = c.get("sparse_embedding") or {}
        raw_scores.append(sum(sparse.get(t, 0.0) for t in query_terms))

    max_score = max(raw_scores) if any(s > 0 for s in raw_scores) else 1.0

    scored = []
    for c, raw in zip(candidates, raw_scores):
        bm25_score = raw / (max_score + 1e-9)
        dense_score = max(0.0, 1.0 - float(c.get("distance", 1.0)))
        hybrid_score = 0.6 * bm25_score + 0.4 * dense_score
        scored.append({**c, "hybrid_score": hybrid_score, "retrieval_type": "hybrid"})

    return sorted(scored, key=lambda x: -x["hybrid_score"])


async def rerank(
        query: str,
        candidates: list[dict],
        image_chunks: list[dict],
        http: httpx.AsyncClient,
        top_n: int | None = None,
        pixel_top_k: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Rerank via Qwen3-VL-Reranker-2B (Modal) or fall back to BM25 if
    RERANK_API_URL is not set.
    Returns (text_chunks, image_chunks) both trimmed to their top-k limits.
    """
    top_n = RERANK_TOP_N if top_n is None else min(top_n, RERANK_TOP_N)
    pixel_top_k = pixel_top_k or PIXEL_TOP_K

    if not USE_MODAL_RERANK:
        return bm25_rerank(query, candidates)[:top_n], image_chunks[:pixel_top_k]

    # Build flat document list — text chunks first, then image chunks
    documents = []
    for c in candidates:
        documents.append({"id": str(c["id"]), "text": c.get("content"), "image": None})

    # Send every candidate image (dense hits + any raster-fallback pages the
    # caller appended) to the reranker rather than pre-trimming to
    # pixel_top_k here: retrieve() already bounds this list to a small,
    # cost-safe size (pixel_top_k * 3 dense hits, plus up to pixel_top_k
    # fallback pages), and fallback pages specifically live at the *end*
    # of that list — slicing to pixel_top_k here would routinely cut them
    # off before the reranker ever saw them. The real top-k cut happens
    # below, driven by the reranker's own relevance ordering.
    original_image_chunks = image_chunks
    trimmed_image_chunks = image_chunks

    for c in trimmed_image_chunks:
        if not c.get("image_path"):
            continue
        try:
            img_path = Path(c["image_path"])
            img_bytes = img_path.read_bytes()
            # MinerU crops/raster fallbacks are always PNG, but candidates
            # here can also be standalone uploads (.jpg/.jpeg) surfaced by
            # image_retrieve()'s standalone_image_fallback — derive the
            # mime from the actual file rather than assuming PNG.
            suffix = img_path.suffix.lstrip(".").lower() or "png"
            mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
            img_b64 = f"data:image/{mime};base64," + base64.b64encode(img_bytes).decode()
        except Exception as e:
            print(f"[rerank] failed to load image {c.get('image_path')}: {e}")
            continue
        # Pass the crop's caption (MinerU's own, or the VLM-fallback gloss —
        # see backend/ingest.py's fill_missing_captions) alongside the image
        # itself: RerankServer's CrossEncoder scores {"text","image"} docs
        # jointly, and a good caption can carry real relevance signal a
        # bare image doesn't (e.g. it names an axis label or a table's
        # subject that isn't visually obvious at reranker resolution).
        documents.append({"id": str(c["id"]), "text": c.get("content"), "image": img_b64})

    try:
        import modal
        RerankServer = modal.Cls.from_name("glyphscholar", "RerankServer")
        resp = await RerankServer().rerank.remote.aio({
            "query": query,
            "documents": documents,
            "top_n": top_n + pixel_top_k,
            "api_key": MODAL_API_KEY,
        })
        ranked = resp["results"]

    except Exception as e:
        print(f"[rerank] Modal reranker failed ({e}), falling back to BM25")
        return bm25_rerank(query, candidates)[:top_n], original_image_chunks[:pixel_top_k]

    # Split ranked results back into text and image by id
    text_by_id = {str(c["id"]): c for c in candidates}
    image_by_id = {str(c["id"]): c for c in trimmed_image_chunks}

    reranked_text = []
    reranked_images = []
    for r in ranked:
        rid = r["id"]
        if rid in text_by_id and len(reranked_text) < top_n:
            reranked_text.append({
                **text_by_id[rid],
                "rerank_score": r["score"],
                "retrieval_type": "rerank",
            })
        elif rid in image_by_id and len(reranked_images) < pixel_top_k:
            reranked_images.append({
                **image_by_id[rid],
                "rerank_score": r["score"],
                "retrieval_type": "pixel",
            })

    return reranked_text, reranked_images


def min_distance(items: list[dict]) -> float | None:
    dists = [c["distance"] for c in items if c.get("distance") is not None]
    return min(dists) if dists else None


def pixel_coverage_ambiguous(text_candidates: list[dict], image_chunks: list[dict]) -> bool:
    """True when neither the text dense-search nor the crop/image
    dense-search came back confident for this query — the query-time
    trigger for rendering a full-page raster on demand (PixelRAG fallback
    case 2; see module docstring). Deliberately requires BOTH sides to be
    weak: a confident text hit alone means the crop index likely just
    doesn't cover this particular page's content type, which is a
    perfectly normal outcome, not a miss worth compensating for.
    """
    text_dist = min_distance(text_candidates)
    pixel_dist = min_distance(image_chunks)
    text_weak = text_dist is None or text_dist > TEXT_FALLBACK_DISTANCE
    pixel_weak = pixel_dist is None or pixel_dist > PIXEL_FALLBACK_DISTANCE
    return text_weak and pixel_weak


async def raster_fallback_for_query(
        session_id: str,
        http: httpx.AsyncClient,
        top_k: int,
        text_candidates: list[dict],
) -> list[dict]:
    """
    Query-time PixelRAG fallback: when both the text chunks and the
    MinerU-crop chunks scored low/ambiguous for this query (see
    pixel_coverage_ambiguous), render the implicated page(s)' full raster
    on demand from the source PDF instead of returning nothing for
    PixelRAG — catches whatever fell through the layout detector's cracks
    (an unusual diagram type, a parsing miss, ...) without having had to
    flag it up front at ingest time.

    Ephemeral by design: rendered/embedded here and handed straight to
    rerank(), not written back to document_chunks — a repeat of the same
    ambiguous query re-renders rather than accumulating duplicate rows.
    """
    pairs: list[tuple[str, int]] = []
    for c in text_candidates[:top_k]:
        page = c.get("metadata", {}).get("page")
        doc_id = c.get("document_id")
        if page and doc_id and (str(doc_id), int(page)) not in pairs:
            pairs.append((str(doc_id), int(page)))
    if not pairs:
        return []
    pairs = pairs[:top_k]

    pool = await get_pool()
    async with pool.acquire() as conn:
        doc_rows = await conn.fetch(
            "SELECT id, file_path FROM documents WHERE id = ANY($1::uuid[])",
            list({p[0] for p in pairs}),
        )
    file_paths = {str(r["id"]): r["file_path"] for r in doc_rows}

    # Local import: keeps backend.retrieve from needing backend.ingest (and
    # its MinerU/pymupdf-heavy dependency surface) on the common path where
    # this fallback never fires.
    from backend.ingest import render_pdf_pages, embed_images

    rendered: list[dict] = []
    for doc_id, page in pairs:
        file_path = file_paths.get(doc_id)
        if not file_path or not Path(file_path).exists():
            continue
        images_dir = Path(file_path).parent / "images" / doc_id
        try:
            page_chunks = await asyncio.to_thread(
                render_pdf_pages, file_path, images_dir, 120, [page]
            )
        except Exception as e:
            print(f"[retrieve] raster fallback render failed for {file_path} p{page}: {e}")
            continue
        for c in page_chunks:
            c["id"] = f"raster-fallback-{doc_id}-{page}"
            c["document_id"] = doc_id
            c["metadata"]["query_time"] = True
            rendered.append(c)

    if not rendered:
        return []

    try:
        img_embeddings = await embed_images([c["image_path"] for c in rendered], http)
    except Exception as e:
        print(f"[retrieve] raster fallback embedding failed (non-fatal): {e}")
        img_embeddings = None

    if img_embeddings:
        for c, emb in zip(rendered, img_embeddings):
            c["image_embedding"] = emb

    return rendered


async def retrieve(
        query: str,
        session_id: str,
        document_ids: list[str] | None,
        http: httpx.AsyncClient,
        include_images: bool = True,
        top_k: int | None = None,
        dense_top_k: int | None = None,
        pixel_top_k: int | None = None,
) -> dict[str, list[dict]]:
    top_k = top_k or FINAL_TOP_K  # falls back to env var defaults
    dense_top_k = dense_top_k or DENSE_TOP_K
    pixel_top_k = pixel_top_k or PIXEL_TOP_K

    query_embedding = await embed_query(query, http)

    candidates = await dense_retrieve(
        query_embedding, session_id, document_ids, top_k=dense_top_k
    )

    image_chunks = []
    fallback_chunks: list[dict] = []
    if include_images:
        image_chunks = await image_retrieve(
            query_embedding, session_id, document_ids, top_k=pixel_top_k * 3
        )
        if pixel_coverage_ambiguous(candidates, image_chunks):
            fallback_chunks = await raster_fallback_for_query(
                session_id, http, top_k=pixel_top_k, text_candidates=candidates
            )
            image_chunks = image_chunks + fallback_chunks

    text_chunks, reranked_images = await rerank(
        query=query,
        candidates=candidates,
        image_chunks=image_chunks,
        http=http,
        top_n=top_k,
        pixel_top_k=pixel_top_k,
    )

    # fallback_chunks were merged into image_chunks above and already went
    # through rerank() like any other image candidate — reranked_images is
    # the authoritative, relevance-ordered result. Just relabel whichever
    # of those (if any) came from the raster-fallback path, for rag_log's
    # benefit, rather than force-including the raw un-reranked fallback
    # chunks (which previously bypassed reranking and could duplicate an
    # entry already present in reranked_images).
    fallback_ids = {str(c["id"]) for c in fallback_chunks}
    for c in reranked_images:
        if str(c["id"]) in fallback_ids:
            c["retrieval_type"] = "raster_fallback"
    image_chunks = reranked_images[:pixel_top_k]

    return {"text_chunks": text_chunks, "image_chunks": image_chunks}
