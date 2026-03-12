"""
Config loader for the Immich Drop Uploader (Python).
Reads ONLY from .env; there is NO runtime mutation from the UI.
"""

from __future__ import annotations
import os
from dataclasses import dataclass
import secrets
from dotenv import load_dotenv


@dataclass
class Settings:
    """App settings loaded from environment variables (.env)."""
    immich_base_url: str
    immich_api_key: str
    max_concurrent: int
    album_name: str = ""
    public_upload_page_enabled: bool = False
    public_base_url: str = ""
    state_db: str = ""
    session_secret: str = ""
    log_level: str = "INFO"
    chunked_uploads_enabled: bool = False
    chunk_size_mb: int = 95
    chunk_root: str = "/data/chunks"

    @property
    def normalized_base_url(self) -> str:
        """Return the base URL without a trailing slash for clean joining and display."""
        return self.immich_base_url.rstrip("/")

def load_settings() -> Settings:
    """Load settings from .env, applying defaults when absent."""
    # Load environment variables from .env once here so importers don’t have to
    try:
        load_dotenv()
    except Exception:
        pass
    base = os.getenv("IMMICH_BASE_URL", "http://127.0.0.1:2283/api")
    api_key = os.getenv("IMMICH_API_KEY", "")
    album_name = os.getenv("IMMICH_ALBUM_NAME", "")
    # Safe defaults: disable public uploader and invites unless explicitly enabled
    def as_bool(v: str, default: bool = False) -> bool:
        if v is None:
            return default
        return str(v).strip().lower() in {"1","true","yes","on"}
    public_upload = as_bool(os.getenv("PUBLIC_UPLOAD_PAGE_ENABLED", "false"), False)
    try:
        maxc = int(os.getenv("MAX_CONCURRENT", "3"))
    except ValueError:
        maxc = 3
    state_db = os.getenv("STATE_DB", "/data/state.db")
    session_secret = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    chunked_uploads_enabled = as_bool(os.getenv("CHUNKED_UPLOADS_ENABLED", "false"), False)
    try:
        chunk_size_mb = int(os.getenv("CHUNK_SIZE_MB", "95"))
    except ValueError:
        chunk_size_mb = 95
    chunk_root = os.getenv("CHUNK_ROOT", "/data/chunks")
    return Settings(
        immich_base_url=base,
        immich_api_key=api_key,
        max_concurrent=maxc,
        album_name=album_name,
        public_upload_page_enabled=public_upload,
        public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
        state_db=state_db,
        session_secret=session_secret,
        log_level=log_level,
        chunked_uploads_enabled=chunked_uploads_enabled,
        chunk_size_mb=chunk_size_mb,
        chunk_root=chunk_root,
    )
