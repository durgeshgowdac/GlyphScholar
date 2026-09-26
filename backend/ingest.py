"""
Document ingestion pipeline for GlyphScholar.

Flow:
  PDF → MinerU API
      → text chunks + PixelRAG visual index
            (MinerU's own figure/table/equation crops, by default —
             full-page rasterization via pymupdf only as a fallback for
             pages those crops don't cover; see render_pdf_pages)
      → nomic-embed-text embeddings (via Ollama)
      → BM25 sparse vectors (rank_bm25)
      → INSERT INTO document_chunks
"""

import asyncio
import base64
import json
import os
import re
import shutil
from pathlib import Path

import httpx
import pymupdf as fitz  # `import fitz` is deprecated as of PyMuPDF 1.24+
from rank_bm25 import BM25Okapi

from backend.db import get_pool, add_document_size_bytes
from backend.mineru_client import MinerUClient

# ── Config ────────────────────────────────────────────────────────────────────
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "Qwen/Qwen3-VL-Embedding-2B")  # Modal (HF repo id)
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")  # Ollama (local pull tag) — text-only
USE_MODAL_EMBED = os.getenv("USE_MODAL_EMBED", "True").lower() in ("true", "1", "yes")
# Fixed to match the two column pairs in document_chunks (see
# backend/migrations/001_app_schema.sql) — modal (Qwen3-VL-Embedding-2B) is
# 2048-dim, local (nomic-embed-text) is 768-dim. Swapping OLLAMA_EMBED_MODEL
# for a different-dimension model needs LOCAL_EMBED_DIM + the *_local
# columns/indexes updated to match.
MODAL_EMBED_DIM = 2048
LOCAL_EMBED_DIM = int(os.getenv("LOCAL_EMBED_DIM", "768"))
EMBED_DIM = MODAL_EMBED_DIM if USE_MODAL_EMBED else LOCAL_EMBED_DIM
EMBED_COL_SUFFIX = "modal" if USE_MODAL_EMBED else "local"
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "Qwen/Qwen3-VL-2B-Instruct")  # Modal — used for caption fallback too

MODAL_API_KEY = os.getenv("MODAL_API_KEY", "")

MINERU_TOKEN = os.getenv("MINERU_TOKEN")
MINERU_URL = os.getenv("MINERU_BASE_URL", "https://mineru.net/api/v4")

# File types sent to MinerU for extraction (see ingest_document below).
# MinerU's Precision Extract API accepts PDF plus these Office formats
# directly — no local conversion needed, same client, same call.
MINERU_FILE_TYPES = {"pdf", "doc", "docx", "ppt", "pptx"}

MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "150"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "256"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "32"))

CHARS_PER_TOKEN = 4
CHUNK_CHARS = CHUNK_SIZE * CHARS_PER_TOKEN
OVERLAP_CHARS = CHUNK_OVERLAP * CHARS_PER_TOKEN

if OVERLAP_CHARS >= CHUNK_CHARS:
    # chunk_text()'s sliding window advances by (CHUNK_CHARS - OVERLAP_CHARS)
    # each iteration — if that's <= 0, `start` never advances and ingestion
    # hangs forever on the first document. Fail fast at import time instead.
    raise ValueError(
        f"CHUNK_OVERLAP ({CHUNK_OVERLAP}) must be smaller than CHUNK_SIZE ({CHUNK_SIZE})"
    )

# Caps concurrent full-page PDF rasterization jobs (render_pdf_pages) across
# ALL sessions in this process — each job is CPU-bound and runs via
# asyncio.to_thread, so this is really a "how many worker threads render
# pages at once" knob. Keep at 1 on constrained/shared hardware; raise on
# a dedicated multi-core box handling concurrent multi-user uploads.
RENDER_CONCURRENCY = int(os.getenv("RENDER_CONCURRENCY", "1"))
RENDER_SEMAPHORE = asyncio.Semaphore(RENDER_CONCURRENCY)

# ── Text chunking ─────────────────────────────────────────────────────────────

# Shared with chainlit_app.app.verify_file_magic so the upload-time check
# and the actual ingestion read agree on what counts as "valid text" —
# strict UTF-8-only rejects (or, if merely lossy-replaced, silently
# mangles) CSVs exported from Excel, which are commonly Windows-1252, not
# UTF-8. utf-8-sig also strips a leading BOM some tools still write.
TEXT_FALLBACK_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252")


def looks_like_text(header: bytes) -> bool:
    """True if `header` decodes cleanly under one of TEXT_FALLBACK_ENCODINGS
    and shows no other binary signal. Used to gate text/csv/json uploads
    without rejecting non-UTF-8-but-still-plain-text files."""
    if b"\x00" in header:
        # Real text essentially never contains NUL bytes; a strong,
        # encoding-independent binary signal.
        return False
    for encoding in TEXT_FALLBACK_ENCODINGS:
        try:
            header.decode(encoding, errors="strict")
            return True
        except UnicodeDecodeError:
            continue
    return False


def decode_text_bytes(raw: bytes) -> str:
    """Decode file bytes for text/csv/json ingestion using the same
    encoding preference order as looks_like_text, so a file that passed
    the upload-time check is read the same way here — not re-decoded as
    strict UTF-8 and silently corrupted. Only reached after
    verify_file_magic already confirmed the bytes decode under one of
    these encodings; the final UTF-8/replace is a defensive fallback that
    should not normally trigger."""
    for encoding in TEXT_FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def chunk_text(text: str, source_meta: dict | None = None) -> list[dict]:
    """Split text into overlapping fixed-size character chunks."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_CHARS
        chunk_text = text[start:end].strip()
        if chunk_text:
            chunks.append({
                "content": chunk_text,
                "chunk_type": "text",
                "metadata": source_meta or {},
            })
        start += CHUNK_CHARS - OVERLAP_CHARS
    return chunks


# ── Sanitize formulae ──────────────────────────────────────────────────────────

LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]")


def normalize_latex_delimiters(text: str) -> str:
    """Normalize one equation-chunk's outer delimiters to $$ / $. Only
    strips delimiters that wrap the ENTIRE chunk (never brackets/parens
    nested inside the formula, e.g. \\left[ \\right], \\bigl( \\bigr)).
    The bare (unescaped) [ ] / ( ) form is only converted when the inner
    content contains a LaTeX command — otherwise ordinary bracketed/
    parenthetical text (citation markers, asides) would get mangled."""
    if not text:
        return text
    stripped = text.strip()

    if stripped.startswith("\\[") and stripped.endswith("\\]"):
        return f"$$\n{stripped[2:-2].strip()}\n$$"
    if stripped.startswith("[") and stripped.endswith("]") and LATEX_CMD_RE.search(stripped):
        return f"$$\n{stripped[1:-1].strip()}\n$$"

    if stripped.startswith("\\(") and stripped.endswith("\\)"):
        return f"${stripped[2:-2].strip()}$"
    if stripped.startswith("(") and stripped.endswith(")") and LATEX_CMD_RE.search(stripped):
        return f"${stripped[1:-1].strip()}$"

    return text


# ── PDF parsing ───────────────────────────────────────────────────────────────

def walk_v2_strings(node) -> list[str]:
    """
    Recursively collect leaf text from a content_list_v2.json block.

    Traverses the structure and collects text from known leaf fields:
    - 'content': Standard text/equation content. For a styled hyperlink
      span this is already the concatenated text of its 'children' (see
      below), so 'children' is skipped rather than walked too — otherwise
      every multi-style hyperlink's text would be collected twice.
    - 'html': Table HTML body.
    - 'math_content': LaTeX interline equations.
    - 'url': Hyperlink targets.

    Ignores structural metadata (e.g., bboxes, types, levels).
    """
    out: list[str] = []
    if isinstance(node, list):
        for item in node:
            out.extend(walk_v2_strings(item))
    elif isinstance(node, dict):
        for key, val in node.items():
            if key == "children":
                # Already represented by this node's own "content" —
                # descending here would duplicate that text per-fragment.
                continue
            if key in ("content", "html", "math_content", "url") and isinstance(val, str):
                out.append(normalize_latex_delimiters(val) if key == "math_content" else val)
            elif isinstance(val, (dict, list)):
                out.extend(walk_v2_strings(val))
    return out


V2_AUXILIARY_TYPES = {
    "page_header", "page_footer", "page_number", "page_aside_text", "page_footnote",
}


def chunks_from_content_list_v2(raw: bytes) -> list[dict]:
    """
    Fallback parser for content_list_v2.json — only used when v1's
    content_list.json isn't present in the zip. v2's top level is a list
    of pages (each itself a list of blocks), so page number comes from
    array position rather than a per-block page_idx field.

    Skips the same layout-noise block types v1's block_text() does, using
    v2's own type names (page_header/page_footer/page_number/
    page_aside_text/page_footnote) — running headers/footers and margin
    notes aren't document content and shouldn't repeat into every page's
    chunk text.
    """
    pages_raw = json.loads(raw)

    chunks = []
    for page_idx, blocks in enumerate(pages_raw):
        parts = []
        for block in blocks:
            if block.get("type") in V2_AUXILIARY_TYPES:
                continue
            strings = walk_v2_strings(block.get("content"))
            text = " ".join(s.strip() for s in strings if s and s.strip())
            if text:
                parts.append(text)

        page_text = "\n\n".join(parts)
        # 0-indexed array position -> 1-indexed to match render_pdf_pages()
        for c in chunk_text(page_text, {"page": page_idx + 1, "source": "mineru_v2"}):
            chunks.append(c)
    return chunks


def block_text(block: dict) -> str:
    """
    Extract indexable text from one content_list.json block, handling the
    type-specific field names actually observed in a real MinerU VLM
    extraction (verified against a real academic-paper output, not just
    the docs):
      - text / ref_text / equation: plain "text" field.
      - table: "table_body" (HTML) plus "table_caption" (list of strings)
        — the caption is often the clearest summary of what the table
        shows, so it's worth indexing even though it was dropped before.
      - code: "code_body" (HTML-ish preformatted text) plus "code_caption"
        — previously dropped entirely, since it has no "text" field at
        all and the old code only special-cased "table".
      - image: no body text at all (the visual content itself is covered
        separately by MinerU's own crop, indexed via crops_from_content_list
        as its own PixelRAG chunk) — but "image_caption" is dense,
        retrievable prose that was being discarded; index that here too so
        it's part of the page's plain-text chunk as well.
      - page_number: pure layout noise (a bare page-number string like
        "4"), not document content — skip it rather than let it pollute
        the page's chunk text.
      - chart: "content" (the chart's data, preserved as Markdown table
        text per MinerU's docs) plus "chart_caption"/"chart_footnote" —
        previously dropped entirely, since chart has no "text" field and
        wasn't special-cased here (crops_from_content_list only ever
        picked up the caption for the image crop, never this table data).
      - list: "list_items" (array of strings) — reference/bibliography
        lists use this type and have no "text" field, so they were being
        silently dropped from the index before this branch existed.
      - anything else (unrecognized future types, ...): fall back to the
        common "text" field if present.
    """
    btype = block.get("type")

    if btype in ("page_number", "header", "footer", "aside_text", "page_footnote"):
        return ""

    if btype == "table":
        caption = " ".join(block.get("table_caption") or [])
        footnote = " ".join(block.get("table_footnote") or [])
        body = block.get("table_body") or ""
        return f"{caption}\n{body}\n{footnote}".strip()

    if btype == "chart":
        caption = " ".join(block.get("chart_caption") or [])
        footnote = " ".join(block.get("chart_footnote") or [])
        body = block.get("content") or ""
        return f"{caption}\n{body}\n{footnote}".strip()

    if btype == "code":
        caption = " ".join(block.get("code_caption") or [])
        footnote = " ".join(block.get("code_footnote") or [])
        body = block.get("code_body") or ""
        return f"{caption}\n{body}\n{footnote}".strip()

    if btype == "image":
        caption = " ".join(block.get("image_caption") or [])
        footnote = " ".join(block.get("image_footnote") or [])
        return f"{caption}\n{footnote}".strip()

    if btype == "list":
        return "\n".join(block.get("list_items") or [])

    return block.get("text") or ""


def chunks_from_content_list(raw: bytes) -> list[dict]:
    """Group MinerU's flat, page-tagged block list into per-page text, then
    run it through the normal chunk_text() sliding window per page — this
    keeps chunks aligned to real page boundaries (needed for PixelRAG's
    text-page → image-page matching in backend/retrieve.py) instead of
    chunking blindly across the whole document."""
    blocks = json.loads(raw)

    pages: dict[int, list[str]] = {}
    for block in blocks:
        page_idx = block.get("page_idx")
        if page_idx is None:
            continue

        text = block_text(block)
        if not text:
            continue

        pages.setdefault(page_idx, []).append(text)

    chunks = []
    for page_idx in sorted(pages):
        page_text = "\n\n".join(pages[page_idx])
        # page_idx is 0-indexed in MinerU's output; store 1-indexed to match
        # render_pdf_pages()'s page numbering (backend/ingest.py) and the
        # filenames it writes (page_0001.png, ...).
        for c in chunk_text(page_text, {"page": page_idx + 1, "source": "mineru"}):
            chunks.append(c)
    return chunks


# ── PixelRAG visual index: MinerU's own crops (primary) ──────────────────────
#
# MinerU already tightly crops every figure, table, and interline equation it
# recognizes (content_list.json's `img_path`, alongside `bbox` and — often —
# a caption). That crop is strictly better PixelRAG signal than a rasterized
# full page: no surrounding unrelated text/whitespace diluting the vision
# embedding, smaller to store, cheaper to embed, and already deterministically
# linked to page/bbox/caption with zero reconstruction work. See
# render_pdf_pages below for the (now conditional) full-page-raster fallback.

CROP_BLOCK_TYPES = ("image", "table", "equation", "chart")
# document_chunks.chunk_type CHECK only allows 'text' | 'image' | 'table' —
# every crop type other than 'table' (equation, chart, plain image) maps to
# 'image'. This is an explicit set, not `if btype == "table" else "image"`,
# so adding a new value to CROP_BLOCK_TYPES above can't silently pass an
# unmapped btype straight through as chunk_type and hit the CHECK again.
CHUNK_TYPE_FOR_BLOCK = {"table": "table"}


def crops_from_content_list(raw: bytes, extract_path: Path, images_dir: Path) -> list[dict]:
    """
    Pull MinerU's own crops for figures, tables, and interline equations out
    of content_list.json (v1 format — the only one observed to carry
    `img_path`/`bbox` per block).

    Each crop is copied out of `extract_path` (MinerU's, possibly temporary,
    extraction dir) into our own persistent `images_dir/crops/` before the
    caller can clean that dir up. `content` is seeded with MinerU's own
    caption when present — that's the retrievable text anchor a query
    actually matches against — and left None otherwise so
    fill_missing_captions() knows which crops still need a fallback gloss.
    """
    blocks = json.loads(raw)
    crops_dir = images_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    crops = []
    for i, block in enumerate(blocks):
        btype = block.get("type")
        if btype not in CROP_BLOCK_TYPES:
            continue

        img_rel_path = block.get("img_path")
        if not img_rel_path:
            # e.g. a table MinerU resolved to HTML (table_body) but never
            # cropped as an image — nothing visual to index for it here.
            continue

        src_path = extract_path / img_rel_path
        if not src_path.is_file():
            print(f"[ingest] crop referenced but missing on disk, skipping: {src_path}")
            continue

        page_idx = block.get("page_idx")
        if page_idx is None:
            continue

        if btype == "table":
            caption = " ".join(block.get("table_caption") or []).strip()
        elif btype == "image":
            caption = " ".join(block.get("image_caption") or []).strip()
        elif btype == "chart":
            caption = " ".join(block.get("chart_caption") or []).strip()
        else:  # interline equation — no *_caption field; the LaTeX itself
            # is the closest thing MinerU gives us to text-side content.
            caption = normalize_latex_delimiters((block.get("text") or "").strip())

        dest_name = f"crop_p{page_idx + 1:04d}_{btype}_{i:04d}{src_path.suffix}"
        dest_path = crops_dir / dest_name
        try:
            shutil.copyfile(src_path, dest_path)
        except OSError as e:
            print(f"[ingest] failed to copy crop {src_path} -> {dest_path}: {e}")
            continue

        crops.append({
            "content": caption or None,
            "chunk_type": CHUNK_TYPE_FOR_BLOCK.get(btype, "image"),
            "image_path": str(dest_path),
            "metadata": {
                "page": page_idx + 1,
                "source": "mineru_crop",
                "block_type": btype,
                "bbox": block.get("bbox"),
                "caption_source": "mineru" if caption else None,
            },
        })

    return crops


async def generate_caption_fallback(image_path: str) -> str | None:
    """
    Cheap one-line VLM gloss for a crop MinerU didn't caption itself.
    Only called for crops whose own image_caption/table_caption came back
    empty — an uncaptioned crop has nothing text-side for a query to match
    against, so this is worth the extra call rather than shipping it bare.
    Uses the same small Qwen3-VL-2B-Instruct AnswerServer already deployed
    for chat, not a separate captioning model. Returns None (crop still
    indexed on image_embedding alone) if no Modal answer model is
    configured, or if the call fails for any reason.
    """
    if not USE_MODAL_EMBED:
        return None

    try:
        raw = await asyncio.to_thread(Path(image_path).read_bytes)
        suffix = Path(image_path).suffix.lstrip(".").lower() or "png"
        mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
        data_uri = f"data:image/{mime};base64," + base64.b64encode(raw).decode()

        import modal
        AnswerServer = modal.Cls.from_name("glyphscholar", "AnswerServer")
        request = {
            "model": ANSWER_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": "One short sentence describing exactly what "
                                             "this figure or table shows. No preamble."},
                ],
            }],
            "max_tokens": 60,
            "stream": True,
            "api_key": MODAL_API_KEY,
        }
        tokens = []
        async for tok in AnswerServer().chat_stream.remote_gen.aio(request):
            tokens.append(tok)
        gloss = "".join(tokens).strip()
        return gloss or None
    except Exception as e:
        print(f"[ingest] caption fallback failed for {image_path}: {e}")
        return None


async def fill_missing_captions(crop_chunks: list[dict]) -> None:
    """Mutates crop_chunks in place: fills `content` for image/table crops
    MinerU left uncaptioned, via generate_caption_fallback(). Interline
    equations are skipped — an empty LaTeX body isn't something a VLM gloss
    meaningfully improves on."""
    targets = [
        c for c in crop_chunks
        if c.get("content") is None and c["metadata"].get("block_type") in ("image", "table")
    ]
    for c in targets:
        gloss = await generate_caption_fallback(c["image_path"])
        if gloss:
            c["content"] = gloss
            c["metadata"]["caption_source"] = "vlm_fallback"


def mineru_parse_sync(file_path: str, images_dir: Path) -> dict:
    """
    Synchronous MinerU extraction via the working client.
    Called via asyncio.to_thread so it doesn't block the event loop.
    Returns {"text_chunks": [...], "crop_chunks": [...]} — crop_chunks are
    MinerU's own figure/table/equation crops (see crops_from_content_list),
    empty when only a v2-only or markdown-only extraction is available
    (those formats aren't observed to carry img_path/bbox per block, so
    there's nothing to crop from — render_pdf_pages' full-page fallback
    picks up the slack for any page that leaves uncovered).
    """
    import tempfile

    client = MinerUClient()  # reads MINERU_TOKEN etc. from env automatically

    with tempfile.TemporaryDirectory() as tmpdir:
        # Override output_dir to a temp location so we don't pollute the project
        client.output_dir = Path(tmpdir)
        client.output_dir.mkdir(parents=True, exist_ok=True)

        results = client.process(file_path)
        extract_dir = results.get(file_path)

        if not extract_dir:
            raise RuntimeError(f"MinerU extraction failed for {file_path}")

        extract_path = Path(extract_dir)

        v2_path = next(
            (p for p in extract_path.rglob("*content_list_v2.json")), None
        )
        v1_path = next(
            (
                p for p in extract_path.rglob("*content_list.json")
                if "content_list_v2.json" not in p.name
            ),
            None,
        )

        # Crops must be copied out of extract_path (inside `tmpdir`) before
        # this `with` block exits and deletes it — do this regardless of
        # which content-list format ends up supplying the text chunks.
        crop_chunks: list[dict] = []
        if v1_path:
            try:
                crop_chunks = crops_from_content_list(v1_path.read_bytes(), extract_path, images_dir)
            except Exception as e:
                print(f"[ingest] crop extraction from {v1_path.name} failed (non-fatal): {e}")

        if v2_path:
            chunks = chunks_from_content_list_v2(v2_path.read_bytes())
            if v1_path:
                try:
                    v1_chunks = chunks_from_content_list(v1_path.read_bytes())
                    v1_pages = len({c["metadata"]["page"] for c in v1_chunks})
                    v2_pages = len({c["metadata"]["page"] for c in chunks})
                    if v1_pages and v2_pages and v1_pages != v2_pages:
                        print(f"[ingest] warning: v2 has {v2_pages} pages, v1 has {v1_pages}")
                except Exception as e:
                    print(f"[ingest] v1/v2 page-count check failed (non-fatal): {e}")
            return {"text_chunks": chunks, "crop_chunks": crop_chunks}

        if v1_path:
            print(f"[ingest] no content_list_v2.json, falling back to {v1_path.name}")
            return {"text_chunks": chunks_from_content_list(v1_path.read_bytes()), "crop_chunks": crop_chunks}

        # Fallback: Markdown (only)
        print(f"[ingest] no content_list JSON found, falling back to .md files")
        chunks = []
        for md_path in extract_path.rglob("*.md"):
            text = md_path.read_text(errors="replace")
            for c in chunk_text(text, {"page": None, "source": "mineru", "page_unknown": True}):
                chunks.append(c)
        return {"text_chunks": chunks, "crop_chunks": []}


async def parse_pdf(file_path: str, images_dir: Path, http: httpx.AsyncClient) -> dict:
    """Parse PDF text + PixelRAG crops via MinerU. Runs the synchronous
    client in a thread. `http` is currently unused but kept in the
    signature for parity with the other async ingestion steps."""
    return await asyncio.to_thread(mineru_parse_sync, file_path, images_dir)


def pdf_page_count(file_path: str) -> int:
    doc = fitz.open(file_path)
    try:
        return doc.page_count
    finally:
        doc.close()


# ── Page image rendering (PixelRAG full-page FALLBACK) ───────────────────────

def render_pdf_pages(
        file_path: str,
        images_dir: Path,
        dpi: int = 120,
        pages: list[int] | None = None,
) -> list[dict]:
    """
    Render PDF pages to PNGs using pymupdf (no poppler dep).
    Returns list of image chunk dicts with image_path + metadata.
    DPI=120 gives good quality at well under 1MB per page; lower for memory savings.

    This is now the PixelRAG FALLBACK path, not the default — MinerU's own
    crops (see crops_from_content_list) are the primary visual index.
    `pages`, when given, restricts rendering to that list of 1-indexed page
    numbers — the pages a document's crops (and text) left with nothing
    indexed for them at all (a layout-detector miss, decorative graphics,
    an unusual diagram type, etc.). Pass None to render every page, used
    only when no coverage information exists at all (e.g. a v2-only or
    markdown-only MinerU extraction — see mineru_parse_sync).
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(file_path)
    image_chunks = []
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    try:
        total_pages = doc.page_count
        page_limit = min(total_pages, MAX_PDF_PAGES)
        truncated = total_pages > MAX_PDF_PAGES
        if truncated:
            print(f"[ingest] {file_path}: {total_pages} pages exceeds MAX_PDF_PAGES="
                  f"{MAX_PDF_PAGES}, rendering only the first {MAX_PDF_PAGES}")

        target_pages = range(1, page_limit + 1) if pages is None else sorted(
            p for p in set(pages) if 1 <= p <= page_limit
        )

        for page_num_1idx in target_pages:
            page = doc[page_num_1idx - 1]
            pix = page.get_pixmap(matrix=mat)
            img_path = images_dir / f"page_{page_num_1idx:04d}.png"
            pix.save(str(img_path))
            pix = None  # explicit free before next page
            image_chunks.append({
                "content": None,
                "chunk_type": "image",
                "image_path": str(img_path),
                "metadata": {
                    "page": page_num_1idx,
                    "source": "pymupdf",
                    "doc_truncated": truncated,
                    "raster_fallback": True,
                },
            })
    finally:
        doc.close()
    return image_chunks


# ── Embeddings ────────────────────────────────────────────────────────────────

async def embed_texts(texts: list[str], http: httpx.AsyncClient) -> list[list[float]]:
    """
    Embed texts via Modal (EMBED_API_URL) when configured, otherwise Ollama.
    Modal endpoint is OpenAI-compatible: POST with {model, input} →
    {data: [{embedding: [...]}]}.
    Ollama endpoint: POST /api/embed with {model, input} →
    {embeddings: [...]}.
    """
    all_embeddings = []
    batch_size = 32

    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]

        if USE_MODAL_EMBED:
            import modal
            EmbeddingServer = modal.Cls.from_name("glyphscholar", "EmbeddingServer")
            resp = await EmbeddingServer().embeddings.remote.aio(
                {"model": EMBED_MODEL, "input": batch, "api_key": MODAL_API_KEY}
            )
            embeddings = [
                item["embedding"]
                for item in sorted(resp["data"], key=lambda x: x["index"])
            ]
            if embeddings and len(embeddings[0]) != EMBED_DIM:
                raise RuntimeError(
                    f"Embedding dimension mismatch: Modal model '{EMBED_MODEL}' "
                    f"returned {len(embeddings[0])}-dim vectors, expected "
                    f"{EMBED_DIM} (MODAL_EMBED_DIM). The deployed Modal model and "
                    f"the *_modal columns/indexes have drifted out of sync — "
                    f"redeploy the model to 2048-dim or update MODAL_EMBED_DIM "
                    f"and the *_modal halfvec columns to match."
                )
            all_embeddings.extend(embeddings)
        else:
            resp = await http.post(
                f"{OLLAMA_BASE_URL}/api/embed",
                json={"model": OLLAMA_EMBED_MODEL, "input": batch},
                timeout=60.0,
            )
            raise_for_ollama_status(resp, OLLAMA_EMBED_MODEL)
            embeddings = resp.json()["embeddings"]

            if embeddings and len(embeddings[0]) != EMBED_DIM:
                raise RuntimeError(
                    f"Embedding dimension mismatch: model returned {len(embeddings[0])}, "
                    f"expected {EMBED_DIM} (LOCAL_EMBED_DIM). document_chunks."
                    f"text_embedding_local is halfvec({EMBED_DIM}) — either set "
                    f"OLLAMA_EMBED_MODEL to a {EMBED_DIM}-dim model, or set "
                    f"LOCAL_EMBED_DIM={len(embeddings[0])} and alter the "
                    f"*_local columns/indexes to match."
                )
            all_embeddings.extend(embeddings)

    return all_embeddings


async def embed_images(image_paths: list[str], http: httpx.AsyncClient) -> list[list[float]] | None:
    """
    Embed images via the vision embedding model (Modal only).
    File I/O is offloaded to a thread to avoid blocking the event loop.

    vLLM's OpenAI-compatible /v1/embeddings only accepts images via its
    chat-style `messages` field — one image per request — NOT as plain
    strings batched into `input` the way text embeddings work (that's the
    schema embed_texts()/embed_query() correctly use). A raw data-URI
    string put in `input` gets tokenized as literal text and instantly
    blows past --max-model-len, which is why this used to fail fast on
    every call rather than actually embedding anything. See
    https://docs.vllm.ai/en/latest/serving/multimodal_inputs.html#embedding
    """
    if not USE_MODAL_EMBED:
        return None

    def load_data_uri(path: str) -> str:
        raw = Path(path).read_bytes()
        suffix = Path(path).suffix.lstrip(".").lower() or "png"
        mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
        return f"data:image/{mime};base64," + base64.b64encode(raw).decode()

    import modal
    EmbeddingServer = modal.Cls.from_name("glyphscholar", "EmbeddingServer")

    async def embed_one(path: str) -> list[float]:
        data_uri = await asyncio.to_thread(load_data_uri, path)
        resp = await EmbeddingServer().embeddings.remote.aio({
            "model": EMBED_MODEL,
            "messages": [{
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": data_uri}}],
            }],
            "api_key": MODAL_API_KEY,
        })
        embedding = resp["data"][0]["embedding"]
        if len(embedding) != MODAL_EMBED_DIM:
            raise RuntimeError(
                f"Embedding dimension mismatch: Modal model '{EMBED_MODEL}' "
                f"returned a {len(embedding)}-dim image vector, expected "
                f"{MODAL_EMBED_DIM} (MODAL_EMBED_DIM)."
            )
        return embedding

    all_embeddings: list[list[float]] = []
    batch_size = 4  # cap in-flight requests, not a single request's batch size anymore
    for i in range(0, len(image_paths), batch_size):
        batch = image_paths[i: i + batch_size]
        all_embeddings.extend(await asyncio.gather(*(embed_one(p) for p in batch)))

    return all_embeddings


def raise_for_ollama_status(resp: httpx.Response, model: str = OLLAMA_EMBED_MODEL) -> None:
    """A bare 404 from Ollama is ambiguous — could be a stale/wrong
    OLLAMA_BASE_URL, Ollama not running, or (most commonly) the model just
    hasn't been pulled yet. Ollama returns 404 for "model not found" on
    /api/embed the same way it does for /api/generate, so surface the
    likely fix instead of a raw httpx.HTTPStatusError."""
    if resp.status_code == 404:
        raise RuntimeError(
            f"Ollama returned 404 for {resp.request.url}. Most likely the "
            f"model '{model}' hasn't been pulled yet — run "
            f"`ollama pull {model}` and confirm it appears in "
            f"`ollama list`. Also double-check OLLAMA_BASE_URL "
            f"({OLLAMA_BASE_URL!r}) points at a running `ollama serve` "
            f"with no trailing /v1."
        )
    resp.raise_for_status()


# ── BM25 sparse vectors ───────────────────────────────────────────────────────

def compute_bm25_vectors(chunks: list[dict]) -> list[dict]:
    """
    Compute BM25 term-weight vectors for all text chunks in a document.
    Stores as {term: weight} dict in sparse_embedding JSONB column.
    Image chunks get None (no text to index).

    Computes each term's weight directly from BM25Okapi's own idf/term-frequency/doc-length
    bookkeeping, rather than calling get_scores() per chunk. Two reasons:
      1. get_scores(query) returns one score PER DOCUMENT in the corpus
         (summed across the query terms treated as one combined query) —
         it does not return one score per term. The previous
         `zip(unique_terms, bm25.get_scores(unique_terms))` was pairing
         each of this chunk's terms with the BM25 relevance of an
         unrelated, positionally-indexed *other* chunk in the same
         document. sparse_embedding was effectively storing noise.
      2. Calling get_scores() once per chunk is also O(n_chunks) work per
         call, i.e. O(n_chunks^2) total for one document — a real cost for
         long PDFs on constrained hardware.
    This version is O(total tokens in the document) and gives each term
    its own correct BM25(term, this_document) weight.
    """
    text_chunks = [c for c in chunks if c["chunk_type"] == "text"]
    if not text_chunks:
        return chunks

    tokenized = [c["content"].lower().split() for c in text_chunks]
    bm25 = BM25Okapi(tokenized)

    text_idx = 0
    for chunk in chunks:
        if chunk["chunk_type"] != "text":
            chunk["sparse_embedding"] = None
            continue

        doc_freqs = bm25.doc_freqs[text_idx]  # {term: term_freq_in_this_doc}
        doc_len = bm25.doc_len[text_idx]
        norm = 1 - bm25.b + bm25.b * (doc_len / bm25.avgdl)

        term_weights = {}
        for term, tf in doc_freqs.items():
            idf = bm25.idf.get(term, 0.0)
            score = idf * (tf * (bm25.k1 + 1)) / (tf + bm25.k1 * norm)
            if score > 0:
                term_weights[term] = float(score)

        # Store top-200 terms by weight to keep JSONB size bounded
        top_terms = sorted(term_weights.items(), key=lambda x: -x[1])[:200]
        chunk["sparse_embedding"] = dict(top_terms)
        text_idx += 1

    return chunks


# ── DB persistence ────────────────────────────────────────────────────────────

async def save_chunks(
        document_id: str,
        session_id: str,
        chunks: list[dict],
) -> None:
    pool = await get_pool()
    records = [
        (
            document_id,
            session_id,
            idx,
            chunk.get("content"),
            chunk.get("chunk_type", "text"),
            chunk.get("image_path"),
            # Format embeddings as halfvec literal strings
            ("[" + ",".join(str(x) for x in chunk["text_embedding"]) + "]")
            if chunk.get("text_embedding") else None,
            ("[" + ",".join(str(x) for x in chunk["image_embedding"]) + "]")
            if chunk.get("image_embedding") else None,
            chunk.get("sparse_embedding") or {},
            chunk.get("metadata") or {},
        )
        for idx, chunk in enumerate(chunks)
    ]
    # Written column names depend on which backend produced these embeddings
    # — modal and local embeddings are never mixed in the same column pair,
    # so switching USE_MODAL_EMBED never needs a dimension migration.
    text_col = f"text_embedding_{EMBED_COL_SUFFIX}"
    image_col = f"image_embedding_{EMBED_COL_SUFFIX}"
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                f"""
                INSERT INTO document_chunks
                (document_id, session_id, chunk_index, content,
                 chunk_type, image_path,
                 {text_col}, {image_col},
                 sparse_embedding, metadata)
                VALUES ($1, $2, $3, $4, $5, $6,
                        $7::halfvec, $8::halfvec,
                        $9, $10)
                """,
                records,
            )


# ── Top-level entry point ─────────────────────────────────────────────────────

async def ingest_document(
        document_id: str,
        session_id: str,
        file_path: str,
        file_type: str,
        images_dir: Path,
        http: httpx.AsyncClient,
) -> int:
    """
    Full ingestion pipeline for a single document.
    Returns the number of chunks inserted.

    file_type: 'pdf' | 'doc' | 'docx' | 'ppt' | 'pptx' | 'image' | 'text' | 'csv' | other
    """
    # Patterns that indicate a chunk is AI boilerplate, not document content
    JUNK_PATTERNS = [
        "as an ai",
        "i cannot access",
        "i'm not able to",
        "i don't have access to",
        "as a language model",
        "i cannot view",
        "i am unable to",
    ]

    def is_junk_chunk(text: str) -> bool:
        lower = text.lower()
        return any(p in lower for p in JUNK_PATTERNS)

    chunks: list[dict] = []

    if file_type in MINERU_FILE_TYPES:
        # 1. Text chunks + PixelRAG crops (figures/tables/interline
        #    equations) via MinerU. No fallback parser — if MinerU is
        #    down/misconfigured we abort rather than half-index the
        #    document (the old "keep the page images anyway" comment here
        #    no longer applied once this raised regardless; see git log).
        try:
            parsed = await parse_pdf(file_path, images_dir, http)
        except Exception as e:
            print(f"[ingest] ERROR: MinerU text extraction failed for {file_path}: {e}")
            raise RuntimeError(f"Text extraction failed, aborting ingestion: {e}")

        text_chunks = parsed["text_chunks"]
        crop_chunks = parsed["crop_chunks"]

        # 1b. Fill in a cheap VLM one-line gloss for any crop MinerU didn't
        # caption itself — see fill_missing_captions' docstring.
        if crop_chunks:
            await fill_missing_captions(crop_chunks)

        # 2. Full-page rasterization is now a FALLBACK, not the default:
        # only for pages that ended up with neither a text chunk nor a
        # MinerU crop at all (a layout-detector miss, decorative graphics,
        # an unusual diagram type, ...) — a much smaller, conditional job
        # instead of rendering every page unconditionally. See
        # render_pdf_pages' docstring for the full reasoning.
        #
        # pdf-only: it renders via pymupdf/fitz, which can't open
        # doc/docx/ppt/pptx — those formats rely on MinerU's own text +
        # crops alone, with no raster fallback for pages it misses.
        image_chunks = crop_chunks
        if file_type == "pdf":
            covered_pages = {
                c["metadata"]["page"]
                for c in text_chunks + crop_chunks
                if c.get("metadata", {}).get("page")
            }
            total_pages = await asyncio.to_thread(pdf_page_count, file_path)
            uncovered_pages = [
                p for p in range(1, min(total_pages, MAX_PDF_PAGES) + 1)
                if p not in covered_pages
            ]

            if uncovered_pages:
                # render_pdf_pages is synchronous/CPU-bound; run it in a worker
                # thread so it doesn't block the single asyncio event loop (which
                # would otherwise freeze chat for every other concurrent user for
                # the duration of rendering a large PDF).
                async with RENDER_SEMAPHORE:
                    fallback_chunks = await asyncio.to_thread(
                        render_pdf_pages, file_path, images_dir, 120, uncovered_pages
                    )
                image_chunks = image_chunks + fallback_chunks

        # Rendered/cropped images can add up to a meaningful chunk of a
        # user's quota — count them against it, not just the source file
        # (see get_user_upload_size_bytes in backend/db.py).
        rendered_bytes = sum(
            Path(c["image_path"]).stat().st_size
            for c in image_chunks
            if c.get("image_path") and Path(c["image_path"]).exists()
        )
        if rendered_bytes:
            await add_document_size_bytes(document_id, rendered_bytes)

        chunks = text_chunks + image_chunks

    elif file_type in ("image",):
        # Single image — treat as one image chunk (no text extraction).
        # No "page" key: this image has no sibling text chunks in the same
        # document, so retrieve.py's page-co-location fallback can never
        # match it (that fallback joins on (document_id, page) against a
        # *text* chunk's page — a standalone upload has none). Mark it
        # "standalone" instead so image_retrieve() can pick it up directly
        # when it has no embedding to search against (non-Modal setups).
        chunks = [{
            "content": None,
            "chunk_type": "image",
            "image_path": file_path,
            "metadata": {"source": "upload", "standalone": True},
        }]

    elif file_type in ("text", "csv"):
        text = decode_text_bytes(Path(file_path).read_bytes())
        chunks = chunk_text(text, {"source": "upload"})

    elif file_type == "json":
        # Index as plain text — good enough for chat-context retrieval
        # without needing a structure-aware JSON parser.
        text = decode_text_bytes(Path(file_path).read_bytes())
        chunks = chunk_text(text, {"source": "upload", "format": "json"})

    else:
        print(f"[ingest] unsupported file_type={file_type!r}, skipping {file_path}")
        return 0

    if not chunks:
        return 0

    # Drop AI-refusal-boilerplate chunks before doing any expensive work on
    # them — filtering after embedding/BM25 (as before) wastes embedding API
    # calls/compute on content that's discarded anyway, and skews BM25's
    # avgdl/idf stats with chunks that never make it into the index.
    chunks = [
        c for c in chunks
        if c.get("chunk_type") != "text" or not is_junk_chunk(c.get("content", ""))
    ]
    if not chunks:
        return 0

    # 3. Embed text chunks
    text_contents = [c["content"] for c in chunks if c.get("chunk_type") == "text" and c.get("content")]
    if text_contents:
        embeddings = await embed_texts(text_contents, http)
        t_idx = 0
        for chunk in chunks:
            if chunk.get("chunk_type") == "text" and chunk.get("content"):
                chunk["text_embedding"] = embeddings[t_idx]
                t_idx += 1

    # 3b. Embed image chunks (PixelRAG visual similarity search) — only
    # possible when a vision embedding model is configured (Modal); on
    # Ollama-only setups this is skipped and retrieve.image_retrieve()
    # falls back to its page-co-location heuristic.
    image_paths = [c["image_path"] for c in chunks if c.get("chunk_type") in ("image", "table") and c.get("image_path")]
    if image_paths:
        try:
            img_embeddings = await embed_images(image_paths, http)
        except Exception as e:
            print(f"[ingest] image embedding failed, PixelRAG will use text-colocation fallback: {e}")
            img_embeddings = None
        if img_embeddings:
            i_idx = 0
            for chunk in chunks:
                if chunk.get("chunk_type") in ("image", "table") and chunk.get("image_path"):
                    chunk["image_embedding"] = img_embeddings[i_idx]
                    i_idx += 1

    # 4. BM25 sparse vectors for text chunks
    # Also synchronous/CPU-bound (and roughly O(n^2) in chunk count — see
    # compute_bm25_vectors' docstring) — keep it off the event loop too.
    chunks = await asyncio.to_thread(compute_bm25_vectors, chunks)

    # 5. Persist to document_chunks
    await save_chunks(document_id, session_id, chunks)

    return len(chunks)
