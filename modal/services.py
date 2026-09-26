"""
GlyphScholar — modal/services.py
=====================================
Qwen3-VL-Embedding-2B + Qwen3-VL-Reranker-2B + Qwen3-VL-2B-Instruct on Modal (SDK 1.0+)

Three Modal classes, each running as its own container group and invoked
via `.remote()` from Python (see the smoke test at the bottom of this file):
  EmbeddingServer.embeddings — text + image embeddings (vLLM pooling runner)
  RerankServer.rerank        — cross-encoder relevance scoring (text/image)
  AnswerServer.chat_stream   — streaming chat completion (vLLM)

Setup:
    pip install modal>=1.0
    modal setup

Create a Modal Secret holding the API key used to authenticate calls to
these services. All three classes read it from the environment at
runtime, injected by Modal from this secret:
    modal secret create glyphscholar-secret MODAL_API_KEY=<your-key>

Download weights into a Modal Volume (run once, ~10GB total):
    modal run modal/services.py::download_models

Deploy:
    modal deploy modal/services.py

Smoke-test against live deployed containers:
    modal run modal/services.py
"""

import os

from dotenv import load_dotenv
load_dotenv()

import modal

# ── Config ─────────────────────────────────────────────────────────────────────

EMBED_MODEL = os.environ.get("EMBED_MODEL", "Qwen/Qwen3-VL-Embedding-2B")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "Qwen/Qwen3-VL-Reranker-2B")
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "Qwen/Qwen3-VL-2B-Instruct")

MODEL_CACHE_DIR = "/cache/hub"
EMBED_GPU_TYPE = os.environ.get("MODAL_EMBED_GPU", "A10G")
RERANK_GPU_TYPE = os.environ.get("MODAL_RERANK_GPU", "A10G")
ANSWER_GPU_TYPE = os.environ.get("MODAL_ANSWER_GPU", "A10G")

EMBED_DIM = int(os.environ.get("EMBED_DIM", "2048"))

# Read locally (e.g. for the smoke test entrypoint below, run from your
# own shell). Inside deployed containers, this same variable is populated
# by the `api_key_secret` Modal Secret attached to each service class.
MODAL_API_KEY = os.environ.get("MODAL_API_KEY", "")

# ── App ────────────────────────────────────────────────────────────────────────

app = modal.App("glyphscholar")

# ── Shared Volume — both services read the same model cache ────────────────────

volume = modal.Volume.from_name("glyphscholar-model-cache", create_if_missing=True)

# ── Shared secret — provides MODAL_API_KEY inside each service's containers ────

api_key_secret = modal.Secret.from_name("glyphscholar-secret")

# ── Shared image ───────────────────────────────────────────────────────────────

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "vllm>=0.19.0",
        "transformers>=4.57.0",
        "sentence-transformers",
        "torch>=2.3.0",
        "accelerate",
        "huggingface_hub",
        "hf_transfer",
        "Pillow",
        "qwen-vl-utils>=0.0.14",
        "fastapi[standard]",
        "httpx",
        "python-dotenv",
    )
    .env({
        "HF_XET_HIGH_PERFORMANCE": "1",
        "HF_HOME": MODEL_CACHE_DIR,
        "VLLM_ATTENTION_BACKEND": "TRITON_ATTN",
        # FlashInfer's top-k/top-p sampler JIT-compiles a CUDA kernel via
        # nvcc on first use. This image only has the CUDA runtime (via
        # torch), not the full toolkit, so that JIT build fails and takes
        # AnswerServer's engine down. Disabling it falls back to vLLM's
        # torch-native sampler, which needs no JIT compilation.
        "VLLM_USE_FLASHINFER_SAMPLER": "0",
    })
)


# ── Embedding service ──────────────────────────────────────────────────────────

@app.cls(
    image=image,
    gpu=EMBED_GPU_TYPE,
    volumes={MODEL_CACHE_DIR: volume},
    secrets=[api_key_secret],
    scaledown_window=300,  # keep warm 5 min after last request, then scale to zero
    timeout=600,
    min_containers=0,  # scale to zero when idle — saves free-tier credits
)
@modal.concurrent(max_inputs=4)
class EmbeddingServer:
    """
    vLLM pooling-runner serving Qwen3-VL-Embedding-2B.
    Proxies requests to an in-container vLLM process via localhost.
    """

    @modal.enter()
    def load(self):
        import subprocess, time
        import httpx as httpx

        self.port = 8001
        self.client = httpx.Client(timeout=120.0)

        cmd = [
            "vllm", "serve", EMBED_MODEL,
            "--runner", "pooling",
            "--port", str(self.port),
            "--host", "0.0.0.0",
            "--max-model-len", "8192",
            "--dtype", "float16",
            "--gpu-memory-utilization", "0.88",
            "--served-model-name", EMBED_MODEL,
            "--trust-remote-code",
            "--download-dir", MODEL_CACHE_DIR,
        ]
        self.proc = subprocess.Popen(cmd)

        # vLLM startup (including torch.compile) can take a few minutes.
        deadline = time.time() + 480
        last_log = time.time()
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"EmbeddingServer: vLLM process exited with code {self.proc.returncode}"
                )
            try:
                r = self.client.get(f"http://localhost:{self.port}/health")
                if r.status_code == 200:
                    print("EmbeddingServer: vLLM ready.")
                    return
            except Exception:
                pass
            if time.time() - last_log > 30:
                elapsed = int(time.time() - (deadline - 480))
                print(f"EmbeddingServer: waiting for vLLM ({elapsed}s elapsed)...")
                last_log = time.time()
            time.sleep(3)

        self.proc.terminate()
        raise RuntimeError("EmbeddingServer: vLLM did not start in time.")

    @modal.exit()
    def shutdown(self):
        self.proc.terminate()
        self.client.close()

    @modal.method()
    def embeddings(self, request: dict):
        import os, hmac
        if not hmac.compare_digest(request.get("api_key") or "", os.environ.get("MODAL_API_KEY", "")):
            raise PermissionError("invalid api key")
        resp = self.client.post(
            f"http://localhost:{self.port}/v1/embeddings",
            json=request,
        )
        resp.raise_for_status()
        return resp.json()


# ── Reranker service ───────────────────────────────────────────────────────────

RERANK_PROMPT = "Retrieve images or text relevant to the user's query."


@app.cls(
    image=image,
    gpu=RERANK_GPU_TYPE,
    volumes={MODEL_CACHE_DIR: volume},
    secrets=[api_key_secret],
    scaledown_window=300,
    timeout=600,
    min_containers=0,
)
@modal.concurrent(max_inputs=2)
class RerankServer:
    """
    Qwen3-VL-Reranker-2B, served via sentence-transformers' CrossEncoder —
    the interface the model was actually released/tested with (it wraps
    the model's Qwen3VLForSequenceClassification head + yes/no classifier
    token internally, rather than us reading logits off a causal LM by
    hand). Handles text-only, image-only, and mixed (text+image) docs.
    """

    @modal.enter()
    def load(self):
        import torch
        from sentence_transformers import CrossEncoder

        print(f"RerankServer: loading {RERANK_MODEL} ...")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.model = CrossEncoder(
            RERANK_MODEL,
            device=self.device,
            trust_remote_code=True,
            cache_folder=MODEL_CACHE_DIR,
            model_kwargs={"torch_dtype": torch.float16},
        )
        print("RerankServer: ready.")

    @staticmethod
    def _to_doc_repr(doc: dict):
        """Build the (text | image-path | {"text":..,"image":..}) shape
        CrossEncoder.predict expects for one document."""
        if not doc.get("image"):
            return doc.get("text") or ""

        import base64
        from io import BytesIO
        from PIL import Image

        raw = doc["image"].split(",", 1)[-1] if "," in doc["image"] else doc["image"]
        pil_image = Image.open(BytesIO(base64.b64decode(raw))).convert("RGB")

        if doc.get("text"):
            return {"text": doc["text"], "image": pil_image}
        return {"image": pil_image}

    @modal.method()
    def rerank(self, request: dict):
        import os, hmac
        if not hmac.compare_digest(request.get("api_key") or "", os.environ.get("MODAL_API_KEY", "")):
            raise PermissionError("invalid api key")

        top_n = request.get("top_n") or len(request["documents"])

        pairs = [
            (request["query"], self._to_doc_repr(doc))
            for doc in request["documents"]
        ]

        # batch_size caps per-batch GPU memory usage for image-heavy batches.
        scores = self.model.predict(pairs, prompt=RERANK_PROMPT, batch_size=4)

        scored = [
            {"id": doc["id"], "score": float(score), "index": i}
            for i, (doc, score) in enumerate(zip(request["documents"], scores))
        ]
        scored.sort(key=lambda x: -x["score"])
        return {"results": scored[:top_n]}


@app.cls(
    image=image,
    gpu=ANSWER_GPU_TYPE,
    volumes={MODEL_CACHE_DIR: volume},
    secrets=[api_key_secret],
    scaledown_window=300,
    timeout=600,
    min_containers=0,
    max_containers=2,
)
class AnswerServer:
    """
    Serves Qwen3-VL-2B-Instruct via vLLM as an OpenAI-compatible chat endpoint.
    Chainlit points its AsyncOpenAI client here; Ollama is the fallback.
    """

    @modal.enter()
    def load(self):
        import subprocess, time
        import httpx as httpx

        self.port = 8002

        cmd = [
            "vllm", "serve", ANSWER_MODEL,
            "--port", str(self.port),
            "--host", "0.0.0.0",
            "--max-model-len", "32768",
            "--dtype", "float16",
            "--gpu-memory-utilization", "0.90",
            "--served-model-name", ANSWER_MODEL,
            "--trust-remote-code",
            "--download-dir", MODEL_CACHE_DIR,
            "--enable-auto-tool-choice",
            "--tool-call-parser", "hermes",
        ]
        self.proc = subprocess.Popen(cmd)
        self.client = httpx.Client(timeout=120.0)
        self.stream_client = httpx.Client(timeout=300.0)

        deadline = time.time() + 480
        last_log = time.time()
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"AnswerServer: vLLM exited with code {self.proc.returncode}"
                )
            try:
                r = self.client.get(f"http://localhost:{self.port}/health")
                if r.status_code == 200:
                    print("AnswerServer: vLLM ready.")
                    return
            except Exception:
                pass
            if time.time() - last_log > 30:
                elapsed = int(time.time() - (deadline - 480))
                print(f"AnswerServer: waiting for vLLM ({elapsed}s elapsed)...")
                last_log = time.time()
            time.sleep(3)

        self.proc.terminate()
        raise RuntimeError("AnswerServer: vLLM did not start in time.")

    @modal.exit()
    def shutdown(self):
        if hasattr(self, "proc"):
            self.proc.terminate()
        if hasattr(self, "client"):
            self.client.close()
        if hasattr(self, "stream_client"):
            self.stream_client.close()

    @modal.method()
    def chat_stream(self, request: dict):
        import os, hmac, json
        if not hmac.compare_digest(request.get("api_key") or "", os.environ.get("MODAL_API_KEY", "")):
            raise PermissionError("invalid api key")

        with self.stream_client.stream(
                "POST", f"http://localhost:{self.port}/v1/chat/completions", json=request, timeout=300.0,
        ) as r:
            for line in r.iter_lines():
                if line and line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        data = json.loads(line[6:])
                        if data["choices"][0]["delta"].get("content"):
                            yield data["choices"][0]["delta"]["content"]
                    except Exception:
                        pass


# ── One-time model download ────────────────────────────────────────────────────

@app.function(
    image=image,
    secrets=[api_key_secret],
    volumes={MODEL_CACHE_DIR: volume},
    timeout=3600,
)
def download_models():
    from huggingface_hub import snapshot_download
    for model_id in [EMBED_MODEL, RERANK_MODEL, ANSWER_MODEL]:
        print(f"Downloading {model_id} ...")
        snapshot_download(model_id, cache_dir=MODEL_CACHE_DIR)
        print(f"Done: {model_id}")
    volume.commit()


# ── Smoke test ─────────────────────────────────────────────────────────────────

@app.local_entrypoint()
def test():
    """
    Run after deploying: modal run modal/services.py
    Calls deployed containers via .remote() — no local GPU needed.
    """
    import math, base64, struct, zlib

    embed = EmbeddingServer()
    rerank = RerankServer()
    answer = AnswerServer()

    failures = []

    def check(label: str, ok: bool):
        print(f"  {'✅' if ok else '❌'} {label}")
        if not ok:
            failures.append(label)

    print("=" * 60)
    print("GlyphScholar Modal Services — Smoke Test")
    print("=" * 60)

    # ── [1] Embeddings — text ────────────────────────────────────────────────
    print("\n[1] Embedding — text...")
    result = embed.embeddings.remote(
        {
            "model": EMBED_MODEL,
            "input": [
                "Represent the user's input. What is self-attention?",
                "Represent the user's input. Self-attention allows tokens to attend to all positions.",
                "Represent the user's input. The Eiffel Tower is located in Paris, France.",
            ],
            "api_key": MODAL_API_KEY,
        }
    )
    embs = [d["embedding"] for d in result["data"]]
    dim = len(embs[0])

    def cos(a, b):
        d = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return d / (na * nb + 1e-9)

    sim_hi = cos(embs[0], embs[1])
    sim_lo = cos(embs[0], embs[2])
    print(f"  dim = {dim}")
    check(f"embedding dim == {EMBED_DIM}", dim == EMBED_DIM)
    print(f"  sim(attention ↔ attention) = {sim_hi:.4f}  (want > 0.7)")
    check("related pair scores higher similarity", sim_hi > 0.7)
    print(f"  sim(attention ↔ Paris)     = {sim_lo:.4f}  (want < 0.5)")
    check("unrelated pair scores lower similarity", sim_lo < 0.5)

    # ── [2] Reranker — text ──────────────────────────────────────────────────
    print("\n[2] Reranker — text documents...")

    rr = rerank.rerank.remote(
        {
            "query": "What is self-attention in transformers?",
            "documents": [
                {
                    "id": "relevant",
                    "text": "Self-attention lets each token attend to all other tokens, computing weighted value sums.",
                },
                {
                    "id": "irrelevant",
                    "text": "The Eiffel Tower was built in 1889 and stands 330 m tall.",
                },
                {
                    "id": "partial",
                    "text": "Attention mechanisms were first used in neural machine translation.",
                },
            ],
            "top_n": 3,
            "api_key": MODAL_API_KEY,
        }
    )

    results = rr["results"]
    print(f"  Order: {[r['id'] for r in results]}")
    check("relevant document ranked first", results[0]["id"] == "relevant")

    # ── [3] Reranker — image ─────────────────────────────────────────────────
    print("\n[3] Reranker — image document...")

    def make_png() -> bytes:
        def chunk(t, d):
            c = t + d
            return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

        scanlines = (b"\x00" + b"\xff\xff\xff" * 4) * 4
        return (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(scanlines))
                + chunk(b"IEND", b"")
        )

    img_b64 = "data:image/png;base64," + base64.b64encode(make_png()).decode()
    rr_img = rerank.rerank.remote(
        {
            "query": "Show me the results table from this paper",
            "documents": [
                {
                    "id": "page-image",
                    "text": None,
                    "image": img_b64,
                },
                {
                    "id": "intro-text",
                    "text": "Introduction: this paper proposes a new method.",
                    "image": None,
                },
            ],
            "top_n": 2,
            "api_key": MODAL_API_KEY,
        }
    )

    img_results = rr_img["results"]
    print(f"  Scores: { {r['id']: round(r['score'], 3) for r in img_results} }")
    check("both text and image documents scored", len(img_results) == 2)

    # ── [4] Reranker — batching (catches drops/dupes across batch_size=4) ────
    print("\n[4] Reranker — batching (7 documents, batch_size=4)...")
    rr_batch = rerank.rerank.remote(
        {
            "query": "self-attention",
            "documents": [
                {
                    "id": f"doc-{i}",
                    "text": f"Document number {i} discusses topic {i} in detail.",
                }
                for i in range(7)
            ],
            "top_n": 7,
            "api_key": MODAL_API_KEY,
        }
    )
    ids_back = [r["id"] for r in rr_batch["results"]]
    expected = {f"doc-{i}" for i in range(7)}
    print(f"  Returned ids: {sorted(ids_back)}")
    check("all 7 documents returned exactly once", sorted(ids_back) == sorted(expected) and len(ids_back) == 7)

    # ── [5] AnswerServer — chat completion ────────────────────────────────────
    print("\n[5] AnswerServer — chat completion...")
    reply = "".join(
        answer.chat_stream.remote_gen({
            "model": ANSWER_MODEL,
            "messages": [
                {"role": "user", "content": "Reply with exactly one word: ok"}
            ],
            "max_tokens": 10,
            "stream": True,
            "api_key": MODAL_API_KEY,
        })
    )

    print(f"  Reply: {reply!r}")
    check("chat completion returned non-empty content", bool(reply.strip()))

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if failures:
        print(f"❌ {len(failures)} check(s) failed:")
        for f in failures:
            print(f"   - {f}")
    else:
        print("✅ All smoke tests passed.")
    print()
    print("Manage / inspect deployed apps at https://modal.com/apps")
    print("=" * 60)

    if failures:
        raise SystemExit(1)
