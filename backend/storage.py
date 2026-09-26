# backend/storage.py
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

import aiofiles
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.logger import logger

# Base upload root — override via env
UPLOAD_ROOT = Path(os.getenv("UPLOAD_ROOT", Path(__file__).resolve().parent.parent / "data/uploads"))

# Where Chainlit-persisted elements (chat images, attachments the data layer
# uploads via create_element) live on disk — separate from UPLOAD_ROOT, which
# is our own app-level document store keyed by identifier/session_id.
ELEMENTS_ROOT = Path(os.getenv("ELEMENTS_ROOT", Path(__file__).resolve().parent.parent / "data/elements"))


class LocalStorageClient(BaseStorageClient):
    """
    Local-filesystem BaseStorageClient for SQLAlchemyDataLayer.

    Chainlit ships S3/Azure/GCS storage_clients but no local-filesystem one —
    this fills that gap for local dev / single-instance self-hosting. Files
    are written under ELEMENTS_ROOT and served back via a route mounted on
    the main FastAPI app (see backend/server.py's "/elements" static mount)
    — NOT via a presigned URL like the cloud providers, since there's no
    object store to presign against.

    Not meant for multi-instance/production deployments: no per-user access
    control on the read URL beyond whatever auth already guards the app, and
    no expiry (unlike storage_expiry_time-based cloud URLs).
    """

    def __init__(self, root: Path = ELEMENTS_ROOT, base_url: str = "/elements"):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")

    def path_for(self, object_key: str) -> Path:
        # object_key is Chainlit-generated ("<user_id>/<element_id>/<name>"),
        # not user input — but resolve+relative_to guards against a future
        # object_key containing ".." from ever escaping self.root.
        dest = (self.root / object_key).resolve()
        dest.relative_to(self.root.resolve())
        return dest

    async def upload_file(
            self,
            object_key: str,
            data: Union[bytes, str],
            mime: str = "application/octet-stream",
            overwrite: bool = True,
            content_disposition: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            dest = self.path_for(object_key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            mode = "w" if isinstance(data, str) else "wb"
            async with aiofiles.open(dest, mode) as f:
                await f.write(data)
            return {"object_key": object_key, "url": f"{self.base_url}/{object_key}"}
        except Exception as e:
            logger.warning(f"LocalStorageClient, upload_file error: {e}")
            return {}

    async def delete_file(self, object_key: str) -> bool:
        try:
            self.path_for(object_key).unlink(missing_ok=True)
            return True
        except Exception as e:
            logger.warning(f"LocalStorageClient, delete_file error: {e}")
            return False

    async def get_read_url(self, object_key: str) -> str:
        return f"{self.base_url}/{object_key}"

    async def close(self) -> None:
        pass


SAFE_CHARS = re.compile(r"[^a-z0-9_]")


def safe_identifier(identifier: str) -> str:
    """
    Defense-in-depth: the portal enforces ^[a-z0-9_]+$ client-side (and the
    app_users table carries a matching CHECK constraint —
    see schema.sql's profiles_username_charset_check and
    backend/migrations/001_app_schema.sql's app_users_username_format),
    but never trust the client — sanitize server-side too.
    """
    cleaned = SAFE_CHARS.sub("_", (identifier or "").strip().lower())
    return cleaned or "anonymous"


def safe_session_id(session_id: str) -> str:
    """Session/thread ids are UUIDs we generate or that Chainlit generates —
    still sanitized before touching the filesystem, same reasoning as above."""
    cleaned = re.sub(r"[^a-z0-9-]", "_", (session_id or "").strip().lower())
    return cleaned or "unknown-session"


def user_upload_dir(identifier: str) -> Path:
    """Returns and creates: data/uploads/<identifier>/"""
    path = UPLOAD_ROOT / safe_identifier(identifier)
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_upload_dir(identifier: str, session_id: str) -> Path:
    """Returns and creates: data/uploads/<identifier>/<session_id>/

    Pass the Chainlit thread id as session_id (see chainlit_app/app.py) so this
    folder name matches the id of the resumable chat thread it belongs to.
    """
    path = UPLOAD_ROOT / safe_identifier(identifier) / safe_session_id(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_upload_path(identifier: str, session_id: str) -> Path:
    """Same path as session_upload_dir(), but does NOT create it. Use this
    for bookkeeping (e.g. storing the eventual path in cl.user_session)
    before any file has actually been uploaded; call session_upload_dir()
    at the point a file is actually being written."""
    return UPLOAD_ROOT / safe_identifier(identifier) / safe_session_id(session_id)
