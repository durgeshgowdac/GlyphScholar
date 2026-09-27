import asyncio
import base64
import json
import os
import re
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import jwt
from chainlit.utils import mount_chainlit
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from jwt import PyJWKClient
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

import backend.db as db
from backend.storage import ELEMENTS_ROOT

load_dotenv()

http_client: httpx.AsyncClient | None = None

RATE_LIMITER_SWEEP_INTERVAL_SECONDS = 3600  # hourly
SESSION_CLEANUP_INTERVAL_SECONDS = 24 * 3600  # daily
SESSION_CLEANUP_OLDER_THAN_DAYS = int(os.getenv("SESSION_CLEANUP_OLDER_THAN_DAYS", "30"))


async def rate_limiter_sweep_loop():
    while True:
        await asyncio.sleep(RATE_LIMITER_SWEEP_INTERVAL_SECONDS)
        try:
            n = login_resolve_limiter.sweep()
            if n:
                print(f"[rate_limiter] swept {n} idle keys")
        except Exception as e:
            print(f"[rate_limiter] sweep failed: {e}")


async def session_cleanup_loop():
    await asyncio.sleep(300)
    while True:
        try:
            n = await db.delete_old_sessions(older_than_days=SESSION_CLEANUP_OLDER_THAN_DAYS)
            if n:
                print(f"[cleanup] removed {n} stale sessions")
        except Exception as e:
            print(f"[cleanup] failed: {e}")
        await asyncio.sleep(SESSION_CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0))
    await db.init_db()
    sweep_task = asyncio.create_task(rate_limiter_sweep_loop())
    cleanup_task = asyncio.create_task(session_cleanup_loop())
    yield
    sweep_task.cancel()
    cleanup_task.cancel()
    await http_client.aclose()
    from chainlit_app.app import close_app_clients
    await close_app_clients()
    await db.close_pool()


app = FastAPI(lifespan=lifespan)

# Ensure the elements directory exists before mounting to prevent RuntimeError
Path(ELEMENTS_ROOT).mkdir(parents=True, exist_ok=True)
app.mount("/elements", StaticFiles(directory=ELEMENTS_ROOT), name="elements")


# --- Minimal in-memory rate limiter -----------------------------------
# NOTE: this is per-process and resets on restart / isn't shared across
# workers. Fine for a single-instance dev/small deployment; if you ever run
# multiple uvicorn workers or replicas, swap this for a shared store
# (Redis) or a proper library (slowapi + a shared backend).
class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self.hits[key]
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()
        if len(hits) >= self.max_calls:
            return False
        hits.append(now)
        return True

    def sweep(self) -> int:
        """Drop any key whose window has fully expired. Call periodically
        (see rate_limiter_sweep_loop in server.py's lifespan) — allow()
        alone only prunes a key's own deque when that same key is hit
        again, so a key that goes idle mid-window would otherwise sit in
        memory forever."""
        now = time.monotonic()
        stale = []
        for key, hits in self.hits.items():
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if not hits:
                stale.append(key)
        for key in stale:
            del self.hits[key]
        return len(stale)


# 5 username-resolution attempts per IP per minute is generous for a real
# user logging in, and slow enough to make enumeration impractical.
login_resolve_limiter = RateLimiter(max_calls=5, window_seconds=60)


class AuthMiddleware:
    """
    Pure ASGI middleware (not the `@app.middleware("http")` decorator, which
    only ever fires for scope["type"] == "http") so that /chat/* is also
    protected on the WebSocket path Chainlit's live session actually uses.
    An HTTP request that fails auth gets redirected to login; a WebSocket
    handshake that fails auth gets its connection refused outright, since
    you can't issue an HTTP redirect mid-upgrade.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        if path == "/" or path.startswith("/auth") or path.startswith("/login") or path == "/chat/favicon":
            await self.app(scope, receive, send)
            return

        # /elements serves chat-uploaded images/attachments (see
        # backend/storage.py's LocalStorageClient) via a plain StaticFiles
        # mount with no access control of its own — it must be gated here
        # the same way /chat is, or any object key (e.g. leaked via a
        # shared conversation, or simply guessed) is servable to anyone,
        # logged in or not. The browser sends the same session cookie on
        # an <img src> request as on any other same-origin request, so
        # this doesn't break normal chat rendering.
        if path.startswith("/chat") or path.startswith("/elements"):
            cookies = parse_cookies_from_scope(scope)
            user = extract_user_from_cookies(cookies)

            if not user:
                print("No valid Supabase session — refusing", scope["type"], path)
                if scope["type"] == "http":
                    request = Request(scope, receive=receive)
                    response = RedirectResponse(portal_url(request, "/auth/login"))
                    await response(scope, receive, send)
                else:
                    # No such thing as an HTTP redirect mid-WebSocket-handshake;
                    # refuse the connection instead.
                    await send({"type": "websocket.close", "code": 4401})
                return

            headers = dict(scope["headers"])
            headers[b"user-id"] = user["user_id"].encode()
            headers[b"user-email"] = user["email"].encode()
            headers[b"username"] = user["username"].encode()
            headers[b"role"] = user["role"].encode()
            scope["headers"] = list(headers.items())

        await self.app(scope, receive, send)


ENVIRONMENT = os.getenv("ENVIRONMENT", "production").lower()
raw_origins = os.getenv(
    "CORS_ORIGINS",
    '["http://localhost:3000","http://127.0.0.1:3000"]'
)
CORS_ORIGINS: list[str] = json.loads(raw_origins)
ALLOWED_PORTAL_HOSTS = {h for h in (urlsplit(o).hostname for o in CORS_ORIGINS) if h}

if ENVIRONMENT == "development":
    # Relaxed CORS for local dev — allow_origin_regex (not "*") is required
    # to combine a wildcard with allow_credentials=True; browsers reject a
    # literal "*" origin when credentials are allowed.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.add_middleware(AuthMiddleware)

# PORTAL_PORT = int(os.getenv("PORTAL_PORT", "3000"))
#
# PORTAL_HOST = os.getenv("PORTAL_HOST")
#
#
# def portal_url(request: Request, path: str = "") -> str:
#     host = PORTAL_HOST or request.url.hostname
#     if host not in ALLOWED_PORTAL_HOSTS:
#         host = next(iter(ALLOWED_PORTAL_HOSTS), "localhost")
#     scheme = request.url.scheme
#     return urlunsplit((scheme, f"{host}:{PORTAL_PORT}", path, "", ""))

# Map hostname -> full origin (scheme + host + port, if any), taken
# straight from CORS_ORIGINS — this is already correct for both
# dev ("http://localhost:3000") and production ("https://glyphscholar.vercel.app").
PORTAL_ORIGINS = {
    urlsplit(o).hostname: o.rstrip("/")
    for o in CORS_ORIGINS
    if urlsplit(o).hostname
}


def portal_url(request: Request, path: str = "") -> str:
    host = request.url.hostname
    origin = PORTAL_ORIGINS.get(host) or next(iter(PORTAL_ORIGINS.values()), "http://localhost:3000")
    return f"{origin}{path}"


SUPABASE_URL = os.getenv("SUPABASE_URL")
if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not set in environment")

SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not set in environment")

JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
jwks_client = PyJWKClient(JWKS_URL)


def get_supabase_cookie_name(supabase_url: str) -> str:
    match = re.search(r"https?://([^.]+)", supabase_url)
    if not match:
        raise ValueError(f"Cannot extract project ref from SUPABASE_URL: {supabase_url!r}")
    ref = match.group(1)
    return f"sb-{ref}-auth-token"


SUPABASE_COOKIE_NAME = get_supabase_cookie_name(SUPABASE_URL)


def verify_supabase_token(token: str) -> dict | None:
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
    except Exception as e:
        print(f"JWT verification failed: {type(e).__name__}: {e}")
        return None


def parse_cookies_from_scope(scope: Scope) -> dict[str, str]:
    """Parse the Cookie header directly from an ASGI scope — works for both
    http and websocket scopes, unlike fastapi.Request.cookies which is only
    available for http requests."""
    headers = dict(scope.get("headers") or [])
    cookie_header = headers.get(b"cookie")
    if not cookie_header:
        return {}
    jar = SimpleCookie()
    jar.load(cookie_header.decode("latin-1"))
    return {k: morsel.value for k, morsel in jar.items()}


def read_supabase_cookie_value(cookies: dict[str, str]) -> str | None:
    """
    Read the Supabase auth cookie, accounting for @supabase/ssr's chunking:
    once the encoded cookie value exceeds ~3180 bytes (easy to hit once
    user_metadata like username/role is embedded in the JWT), the browser
    client splits it into "<name>.0", "<name>.1", ... instead of storing it
    under the plain name. Reading only the plain name (as before) meant
    sessions with larger tokens would silently fail auth.
    """
    single = cookies.get(SUPABASE_COOKIE_NAME)
    if single is not None:
        return single

    chunks = []
    i = 0
    while True:
        chunk = cookies.get(f"{SUPABASE_COOKIE_NAME}.{i}")
        if chunk is None:
            break
        chunks.append(chunk)
        i += 1

    return "".join(chunks) if chunks else None


def extract_user_from_cookies(cookies: dict[str, str]) -> dict | None:
    """Verify the Supabase browser cookie and return the user, or None."""
    raw = read_supabase_cookie_value(cookies)
    if not raw:
        return None
    try:
        padded = raw.removeprefix("base64-")
        missing = (4 - len(padded) % 4) % 4
        decoded = base64.b64decode(padded + "=" * missing).decode()

        token_data = json.loads(decoded)
        if isinstance(token_data, list):
            # Older @supabase/ssr versions stored [access_token, refresh_token, ...]
            access_token = token_data[0] if token_data else None
        elif isinstance(token_data, dict):
            access_token = token_data.get("access_token")
        else:
            print(f"[extract_user] unexpected cookie payload type: {type(token_data)}")
            return None

    except Exception:
        return None

    user = verify_supabase_token(access_token)
    if not user:
        return None

    return {
        "user_id": user["sub"],
        "email": user.get("email", ""),
        "username": user.get("user_metadata", {}).get("username", ""),
        "role": user.get("app_metadata", {}).get("role", ""),
    }


def extract_user_from_request(request: Request) -> dict | None:
    """Read and verify the Supabase browser cookie on a plain HTTP request."""
    return extract_user_from_cookies(dict(request.cookies))


def supabase_cookie_names_present(cookies: dict[str, str]) -> list[str]:
    """
    Return every cookie name actually present that belongs to the Supabase
    session — the plain name if unchunked, or all "<name>.0", "<name>.1", ...
    chunks if @supabase/ssr split it. Used so that anywhere we log a user
    out, we clear ALL of it, not just the base name (which silently leaves
    a valid session behind when the token was large enough to be chunked).
    """
    if SUPABASE_COOKIE_NAME in cookies:
        return [SUPABASE_COOKIE_NAME]

    names = []
    i = 0
    while f"{SUPABASE_COOKIE_NAME}.{i}" in cookies:
        names.append(f"{SUPABASE_COOKIE_NAME}.{i}")
        i += 1
    return names


@app.get("/")
async def root(request: Request):
    return RedirectResponse(portal_url(request))


@app.get("/login")
async def chainlit_login_redirect(request: Request):
    return RedirectResponse(portal_url(request, "/auth/login"), status_code=302)


@app.post("/auth/resolve-login")
async def resolve_login(request: Request):
    """
    Resolve a login identifier (email or username) to an email address,
    server-side, using the service_role key. This is the ONLY sanctioned
    caller of the get_email_from_username RPC — it is not granted to
    anon/authenticated in Postgres (see schema.sql), specifically so the
    public anon key can never be used to enumerate emails directly against
    Supabase's REST API.

    Always returns 200 with {"email": ...} or {"email": null} — never
    reveals via status code / timing-obvious means whether the username
    exists, beyond the rate limit itself.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not login_resolve_limiter.allow(client_ip):
        return JSONResponse(
            status_code=429,
            content={"error": "Too many attempts. Please wait a minute and try again."},
        )

    body = await request.json()
    identifier = (body.get("identifier") or "").strip().lower()
    if not identifier:
        return JSONResponse(status_code=400, content={"error": "identifier is required"})

    if "@" in identifier:
        # Already an email — no lookup needed.
        return JSONResponse({"email": identifier})

    try:
        resp = await http_client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/get_email_from_username",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
            json={"p_username": identifier},
        )
        resp.raise_for_status()
        email = resp.json()  # RPC returns the scalar text (or null) directly
    except Exception as e:
        print(f"resolve-login lookup failed: {type(e).__name__}: {e}")
        return JSONResponse({"email": None})

    return JSONResponse({"email": email})


@app.get("/auth/check")
async def check_session(request: Request):
    user = extract_user_from_request(request)
    if not user:
        return JSONResponse(status_code=401, content={"ok": False})
    return JSONResponse({"ok": True})


@app.get("/auth/logout")
async def logout(request: Request):
    response = RedirectResponse(
        portal_url(request, "/auth/logout-callback"),
        status_code=302
    )

    # hostname = request.url.hostname
    # response.delete_cookie(SUPABASE_COOKIE_NAME, path="/", domain=hostname, samesite="lax")

    response.delete_cookie(SUPABASE_COOKIE_NAME, path="/", samesite="lax")
    # Also clear any chunked variants — deleting only the base name leaves
    # .0/.1/... behind if the session was ever large enough to get split.
    for name in supabase_cookie_names_present(dict(request.cookies)):
        response.delete_cookie(name, path="/", samesite="lax")

    return response


@app.get("/assets/{file_path:path}")
async def redirect_root_assets(file_path: str):
    return RedirectResponse(url=f"/chat/assets/{file_path}")


@app.get("/local-files/{document_id}")
async def serve_local_file(document_id: str, request: Request):
    user = extract_user_from_request(request)
    if not user:
        return Response(status_code=401)

    try:
        uuid.UUID(document_id)
    except ValueError:
        return Response(status_code=404)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_path, filename, mime_type FROM documents "
            "WHERE id = $1 AND user_id = $2",
            document_id, user["user_id"],
        )

    if not row or not Path(row["file_path"]).exists():
        return Response(status_code=404)

    return FileResponse(
        row["file_path"],
        media_type=row["mime_type"] or "application/pdf",
        filename=row["filename"],
    )


PUBLIC_DIR = Path(__file__).resolve().parent / "public"


@app.get("/chat/favicon", include_in_schema=False)
async def chainlit_favicon():
    return FileResponse(
        PUBLIC_DIR / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},
    )


from chainlit.config import public_dir

print(f"Chainlit public directory: {public_dir}")

mount_chainlit(app=app, target="chainlit_app/app.py", path="/chat")
