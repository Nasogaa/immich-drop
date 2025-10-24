"""
Immich Drop Uploader – Backend (FastAPI, simplified)
----------------------------------------------------
- Serves static frontend (no settings UI)
- Uploads to Immich using values from .env ONLY
- Duplicate checks (local SHA-1 DB + optional Immich bulk-check)
- WebSocket progress per session
- Ephemeral "Connected" banner via /api/ping
"""

from __future__ import annotations

import asyncio
import io
import json
import hashlib
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
import logging
from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from starlette.middleware.sessions import SessionMiddleware
from PIL import Image, ExifTags
try:
    import qrcode
except Exception:
    qrcode = None

from app.config import Settings, load_settings

# ---- App & static ----
app = FastAPI(title="Immich Drop Uploader (Python)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global settings (read-only at runtime)
SETTINGS: Settings = load_settings()

# Basic logging setup using settings
logging.basicConfig(level=SETTINGS.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("immich_drop")

# Cookie-based session for short-lived auth token storage (no persistence)
app.add_middleware(SessionMiddleware, secret_key=SETTINGS.session_secret, same_site="lax")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Chunk upload storage
CHUNK_ROOT = "/data/chunks"
try:
    os.makedirs(CHUNK_ROOT, exist_ok=True)
except Exception:
    pass

# Album cache
ALBUM_ID: Optional[str] = None

def reset_album_cache() -> None:
    """Invalidate the cached Immich album id so next use re-resolves it."""
    global ALBUM_ID
    ALBUM_ID = None

# ---------- DB (local dedupe cache) ----------

def db_init() -> None:
    """Create the local SQLite table used for duplicate checks (idempotent)."""
    conn = sqlite3.connect(SETTINGS.state_db)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checksum TEXT UNIQUE,
            filename TEXT,
            size INTEGER,
            device_asset_id TEXT,
            immich_asset_id TEXT,
            created_at TEXT,
            inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT,
            uploadedat TEXT DEFAULT CURRENT_TIMESTAMP,
            ip TEXT,
            useragent TEXT,
            fingerprint TEXT,
            filename TEXT,
            size INTEGER,
            checksum TEXT,
            immichassetid TEXT
        );
        """
    )
    conn.commit()
    conn.close()

def db_lookup_checksum(checksum: str) -> Optional[dict]:
    """Return a record for the given checksum if seen before (None if not)."""
    conn = sqlite3.connect(SETTINGS.state_db)
    cur = conn.cursor()
    cur.execute("SELECT checksum, immich_asset_id FROM uploads WHERE checksum = ?", (checksum,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"checksum": row[0], "immich_asset_id": row[1]}
    return None

def db_lookup_device_asset(device_asset_id: str) -> bool:
    """True if a deviceAssetId has been uploaded by this service previously."""
    conn = sqlite3.connect(SETTINGS.state_db)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM uploads WHERE device_asset_id = ?", (device_asset_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row)

def db_insert_upload(checksum: str, filename: str, size: int, device_asset_id: str, immich_asset_id: Optional[str], created_at: str) -> None:
    """Insert a newly-uploaded asset into the local cache (ignore on duplicates)."""
    conn = sqlite3.connect(SETTINGS.state_db)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO uploads (checksum, filename, size, device_asset_id, immich_asset_id, created_at) VALUES (?,?,?,?,?,?)",
        (checksum, filename, size, device_asset_id, immich_asset_id, created_at)
    )
    conn.commit()
    conn.close()

db_init()

# ---------- WebSocket hub ----------

class SessionHub:
    """Holds WebSocket connections per session and broadcasts progress updates."""
    def __init__(self) -> None:
        self.sessions: Dict[str, List[WebSocket]] = {}

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        """Register a newly accepted WebSocket under the given session id."""
        self.sessions.setdefault(session_id, []).append(ws)

    def _cleanup_closed(self, session_id: str) -> None:
        """Drop closed sockets and cleanup empty session buckets."""
        if session_id not in self.sessions:
            return
        self.sessions[session_id] = [w for w in self.sessions[session_id] if w.client_state == WebSocketState.CONNECTED]
        if not self.sessions[session_id]:
            del self.sessions[session_id]

    async def send(self, session_id: str, payload: dict) -> None:
        """Broadcast a JSON payload to all sockets for one session."""
        conns = self.sessions.get(session_id, [])
        for ws in list(conns):
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                try:
                    await ws.close()
                except Exception:
                    pass
        self._cleanup_closed(session_id)

    async def disconnect(self, session_id: str, ws: WebSocket) -> None:
        """Remove a socket from the hub and close it (best-effort)."""
        if session_id in self.sessions and ws in self.sessions[session_id]:
            self.sessions[session_id].remove(ws)
            self._cleanup_closed(session_id)
        # Only try to close if the connection is still open
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close()
            except Exception:
                pass

hub = SessionHub()

# ---------- Helpers ----------

def sha1_hex(file_bytes: bytes) -> str:
    """Return SHA-1 hex digest of file_bytes."""
    h = hashlib.sha1()
    h.update(file_bytes)
    return h.hexdigest()

def sanitize_filename(name: Optional[str]) -> str:
    """Return a minimally sanitized filename that preserves the original name.

    - Removes control characters (\x00-\x1F, \x7F)
    - Replaces path separators ('/' and '\\') with underscore
    - Falls back to 'file' if empty
    Other Unicode characters and spaces are preserved.
    """
    if not name:
        return "file"
    cleaned_chars = []
    for ch in str(name):
        o = ord(ch)
        if o < 32 or o == 127:
            continue
        if ch in ('/', '\\'):
            cleaned_chars.append('_')
        else:
            cleaned_chars.append(ch)
    cleaned = ''.join(cleaned_chars).strip()
    return cleaned or "file"

def read_exif_datetimes(file_bytes: bytes):
    """
    Extract EXIF DateTimeOriginal / ModifyDate values when possible.
    Returns (created, modified) as datetime or (None, None) on failure.
    """
    created = modified = None
    try:
        with Image.open(io.BytesIO(file_bytes)) as im:
            exif = getattr(im, "_getexif", lambda: None)() or {}
            if exif:
                tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
                dt_original = tags.get("DateTimeOriginal") or tags.get("CreateDate")
                dt_modified = tags.get("ModifyDate") or dt_original
                def parse_dt(s: str):
                    try:
                        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
                    except Exception:
                        return None
                if isinstance(dt_original, str):
                    created = parse_dt(dt_original)
                if isinstance(dt_modified, str):
                    modified = parse_dt(dt_modified)
    except Exception:
        pass
    return created, modified

def immich_headers(request: Optional[Request] = None) -> dict:
    """Headers for Immich API calls using either session access token or API key."""
    headers = {"Accept": "application/json"}
    token = None
    try:
        if request is not None:
            token = request.session.get("accessToken")
    except Exception:
        token = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif SETTINGS.immich_api_key:
        headers["x-api-key"] = SETTINGS.immich_api_key
    return headers

def get_or_create_album(request: Optional[Request] = None, album_name_override: Optional[str] = None) -> Optional[str]:
    """Get existing album by name or create a new one. Returns album ID or None."""
    global ALBUM_ID
    album_name = album_name_override if album_name_override is not None else SETTINGS.album_name
    # Skip if no album name configured
    if not album_name:
        return None
    # Return cached album ID if already fetched and using default settings name
    if album_name_override is None and ALBUM_ID:
        return ALBUM_ID
    try:
        # First, try to find existing album
        url = f"{SETTINGS.normalized_base_url}/albums"
        r = requests.get(url, headers=immich_headers(request), timeout=10)
        
        if r.status_code == 200:
            albums = r.json()
            for album in albums:
                if album.get("albumName") == album_name:
                    found_id = album.get("id")
                    if album_name_override is None:
                        ALBUM_ID = found_id
                        logger.info(f"Found existing album '%s' with ID: %s", album_name, ALBUM_ID)
                        return ALBUM_ID
                    else:
                        return found_id
        
        # Album doesn't exist, create it
        create_url = f"{SETTINGS.normalized_base_url}/albums"
        payload = {
            "albumName": album_name,
            "description": "Auto-created album for Immich Drop uploads"
        }
        r = requests.post(create_url, headers={**immich_headers(request), "Content-Type": "application/json"}, 
                          json=payload, timeout=10)
        
        if r.status_code in (200, 201):
            data = r.json()
            new_id = data.get("id")
            if album_name_override is None:
                ALBUM_ID = new_id
                logger.info("Created new album '%s' with ID: %s", album_name, ALBUM_ID)
                return ALBUM_ID
            else:
                logger.info("Created new album '%s' with ID: %s", album_name, new_id)
                return new_id
        else:
            logger.warning("Failed to create album: %s - %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("Error managing album: %s", e)
    
    return None

def add_asset_to_album(asset_id: str, request: Optional[Request] = None, album_id_override: Optional[str] = None, album_name_override: Optional[str] = None) -> bool:
    """Add an asset to the configured album. Returns True on success."""
    album_id = album_id_override
    if not album_id:
        album_id = get_or_create_album(request=request, album_name_override=album_name_override)
    if not album_id or not asset_id:
        return False
    
    try:
        url = f"{SETTINGS.normalized_base_url}/albums/{album_id}/assets"
        payload = {"ids": [asset_id]}
        r = requests.put(url, headers={**immich_headers(request), "Content-Type": "application/json"}, 
                         json=payload, timeout=10)
        
        if r.status_code == 200:
            results = r.json()
            # Check if any result indicates success
            for result in results:
                if result.get("success"):
                    return True
                elif result.get("error") == "duplicate":
                    # Asset already in album, consider it success
                    return True
        return False
    except Exception as e:
        logger.exception("Error adding asset to album: %s", e)
        return False

def immich_ping() -> bool:
    """Best-effort reachability check against a few Immich endpoints."""
    if not SETTINGS.immich_api_key:
        return False
    base = SETTINGS.normalized_base_url
    for path in ("/server-info", "/server/version", "/users/me"):
        try:
            r = requests.get(f"{base}{path}", headers=immich_headers(), timeout=4)
            if 200 <= r.status_code < 400:
                return True
        except Exception:
            continue
    return False

def immich_bulk_check(checks: List[dict]) -> Dict[str, dict]:
    """Try Immich bulk upload check; return map id->result (or empty on failure)."""
    try:
        url = f"{SETTINGS.normalized_base_url}/assets/bulk-upload-check"
        r = requests.post(url, headers=immich_headers(), json={"assets": checks}, timeout=10)
        if r.status_code == 200:
            results = r.json().get("results", [])
            return {x["id"]: x for x in results}
    except Exception:
        pass
    return {}

async def send_progress(session_id: str, item_id: str, status: str, progress: int = 0, message: str = "", response_id: Optional[str] = None) -> None:
    """Push a progress update over WebSocket for one queue item."""
    await hub.send(session_id, {
        "item_id": item_id,
        "status": status,
        "progress": progress,
        "message": message,
        "responseId": response_id,
    })

# ---------- Routes ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Serve the SPA (frontend/index.html) or redirect to login if disabled."""
    if not SETTINGS.public_upload_page_enabled:
        return RedirectResponse(url="/login")
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

@app.get("/login", response_class=HTMLResponse)
async def login_page(_: Request) -> HTMLResponse:
    """Serve the login page."""
    return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))

@app.get("/menu", response_class=HTMLResponse)
async def menu_page(request: Request) -> HTMLResponse:
    """Serve the menu page for creating invite links. Requires login."""
    if not request.session.get("accessToken"):
        return RedirectResponse(url="/login")
    return FileResponse(os.path.join(FRONTEND_DIR, "menu.html"))

@app.get("/favicon.ico")
async def favicon() -> Response:
    """Serve favicon from /static/favicon.png if present (avoids 404 noise)."""
    path = os.path.join(FRONTEND_DIR, "favicon.png")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return Response(content=f.read(), media_type="image/png")
    return Response(status_code=204)

@app.post("/api/ping")
async def api_ping() -> dict:
    """Connectivity test endpoint used by the UI to display a temporary banner."""
    return {
        "ok": immich_ping(), 
        "base_url": SETTINGS.normalized_base_url,
        "album_name": SETTINGS.album_name if SETTINGS.album_name else None
    }

@app.get("/api/config")
async def api_config() -> dict:
    """Expose minimal public configuration flags for the frontend."""
    return {
        "public_upload_page_enabled": SETTINGS.public_upload_page_enabled,
        "chunked_uploads_enabled": SETTINGS.chunked_uploads_enabled,
        "chunk_size_mb": SETTINGS.chunk_size_mb,
    }

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """WebSocket endpoint for pushing per-item upload progress."""
    await ws.accept()
    try:
        init = await ws.receive_text()
        data = json.loads(init)
        session_id = data.get("session_id") or "default"
    except Exception:
        session_id = "default"
    # If this is the first socket for a (possibly new) session, reset album cache
    # so a freshly opened page can rotate the drop album by renaming the old one.
    if session_id not in hub.sessions:
        reset_album_cache()
    await hub.connect(session_id, ws)

    # keepalive to avoid proxy idle timeouts
    try:
        while True:
            msg_task = asyncio.create_task(ws.receive_text())
            keep_task = asyncio.create_task(asyncio.sleep(30))
            done, pending = await asyncio.wait({msg_task, keep_task}, return_when=asyncio.FIRST_COMPLETED)
            if msg_task in done:
                _ = msg_task.result()
            else:
                await ws.send_text('{"type":"ping"}')
            for t in pending:
                t.cancel()
    except WebSocketDisconnect:
        await hub.disconnect(session_id, ws)

@app.post("/api/upload")
async def api_upload(
    request: Request,
    file: UploadFile,
    item_id: str = Form(...),
    session_id: str = Form(...),
    last_modified: Optional[int] = Form(None),
    invite_token: Optional[str] = Form(None),
    fingerprint: Optional[str] = Form(None),
):
    """Receive a file, check duplicates, forward to Immich; stream progress via WS."""
    raw = await file.read()
    size = len(raw)
    checksum = sha1_hex(raw)

    exif_created, exif_modified = read_exif_datetimes(raw)
    created_at = exif_created or (datetime.fromtimestamp(last_modified / 1000) if last_modified else datetime.utcnow())
    modified_at = exif_modified or created_at
    created_iso = created_at.isoformat()
    modified_iso = modified_at.isoformat()

    device_asset_id = f"{file.filename}-{last_modified or 0}-{size}"

    if db_lookup_checksum(checksum):
        await send_progress(session_id, item_id, "duplicate", 100, "Duplicate (by checksum - local cache)")
        return JSONResponse({"status": "duplicate", "id": None}, status_code=200)
    if db_lookup_device_asset(device_asset_id):
        await send_progress(session_id, item_id, "duplicate", 100, "Already uploaded from this device (local cache)")
        return JSONResponse({"status": "duplicate", "id": None}, status_code=200)

    await send_progress(session_id, item_id, "checking", 2, "Checking duplicates…")
    bulk = immich_bulk_check([{"id": item_id, "checksum": checksum}])
    if bulk.get(item_id, {}).get("action") == "reject" and bulk[item_id].get("reason") == "duplicate":
        asset_id = bulk[item_id].get("assetId")
        db_insert_upload(checksum, file.filename, size, device_asset_id, asset_id, created_iso)
        await send_progress(session_id, item_id, "duplicate", 100, "Duplicate (server)", asset_id)
        return JSONResponse({"status": "duplicate", "id": asset_id}, status_code=200)

    safe_name = sanitize_filename(file.filename)
    def gen_encoder() -> MultipartEncoder:
        return MultipartEncoder(fields={
            "assetData": (safe_name, io.BytesIO(raw), file.content_type or "application/octet-stream"),
            "deviceAssetId": device_asset_id,
            "deviceId": f"python-{session_id}",
            "fileCreatedAt": created_iso,
            "fileModifiedAt": modified_iso,
            "isFavorite": "false",
            "filename": safe_name,
            "originalFileName": safe_name,
        })

    encoder = gen_encoder()

    # Invite token validation (if provided)
    target_album_id: Optional[str] = None
    target_album_name: Optional[str] = None
    if invite_token:
        try:
            conn = sqlite3.connect(SETTINGS.state_db)
            cur = conn.cursor()
            cur.execute("SELECT token, album_id, album_name, max_uses, used_count, expires_at, COALESCE(claimed,0), claimed_by_session, password_hash, COALESCE(disabled,0) FROM invites WHERE token = ?", (invite_token,))
            row = cur.fetchone()
            conn.close()
        except Exception as e:
            logger.exception("Invite lookup error: %s", e)
            row = None
        if not row:
            await send_progress(session_id, item_id, "error", 100, "Invalid invite token")
            return JSONResponse({"error": "invalid_invite"}, status_code=403)
        _, album_id, album_name, max_uses, used_count, expires_at, claimed, claimed_by_session, password_hash, disabled = row
        # Admin deactivation check
        try:
            if int(disabled) == 1:
                await send_progress(session_id, item_id, "error", 100, "Invite disabled")
                return JSONResponse({"error": "invite_disabled"}, status_code=403)
        except Exception:
            pass
        # If invite requires password, ensure this session is authorized
        if password_hash:
            try:
                ia = request.session.get("inviteAuth") or {}
                if not ia.get(invite_token):
                    await send_progress(session_id, item_id, "error", 100, "Password required")
                    return JSONResponse({"error": "invite_password_required"}, status_code=403)
            except Exception:
                await send_progress(session_id, item_id, "error", 100, "Password required")
                return JSONResponse({"error": "invite_password_required"}, status_code=403)
        # Expiry check
        if expires_at:
            try:
                if datetime.utcnow() > datetime.fromisoformat(expires_at):
                    await send_progress(session_id, item_id, "error", 100, "Invite expired")
                    return JSONResponse({"error": "invite_expired"}, status_code=403)
            except Exception:
                pass
        # One-time claim or multi-use enforcement
        try:
            max_uses_int = int(max_uses) if max_uses is not None else -1
        except Exception:
            max_uses_int = -1
        if max_uses_int == 1:
            # Already claimed?
            if claimed:
                # Allow same session to continue; block different sessions
                if claimed_by_session and claimed_by_session != session_id:
                    await send_progress(session_id, item_id, "error", 100, "Invite already used")
                    return JSONResponse({"error": "invite_claimed"}, status_code=403)
                # claimed by same session (or unknown): allow
            else:
                # Atomically claim the one-time invite to prevent concurrent use
                try:
                    connc = sqlite3.connect(SETTINGS.state_db)
                    curc = connc.cursor()
                    curc.execute(
                        "UPDATE invites SET claimed = 1, claimed_at = CURRENT_TIMESTAMP, claimed_by_session = ? WHERE token = ? AND (claimed IS NULL OR claimed = 0)",
                        (session_id, invite_token)
                    )
                    connc.commit()
                    changed = connc.total_changes
                    connc.close()
                except Exception as e:
                    logger.exception("Invite claim failed: %s", e)
                    return JSONResponse({"error": "invite_claim_failed"}, status_code=500)
                if changed == 0:
                    # Someone else just claimed; re-check owner
                    try:
                        conn2 = sqlite3.connect(SETTINGS.state_db)
                        cur2 = conn2.cursor()
                        cur2.execute("SELECT claimed_by_session FROM invites WHERE token = ?", (invite_token,))
                        owner_row = cur2.fetchone()
                        conn2.close()
                        owner = owner_row[0] if owner_row else None
                    except Exception:
                        owner = None
                    if not owner or owner != session_id:
                        await send_progress(session_id, item_id, "error", 100, "Invite already used")
                        return JSONResponse({"error": "invite_claimed"}, status_code=403)
        else:
            # Usage check for multi-use (max_uses < 0 => indefinite)
            if (used_count or 0) >= (max_uses_int if max_uses_int >= 0 else 10**9):
                await send_progress(session_id, item_id, "error", 100, "Invite already used up")
                return JSONResponse({"error": "invite_exhausted"}, status_code=403)
        target_album_id = album_id
        target_album_name = album_name

    async def do_upload():
        await send_progress(session_id, item_id, "uploading", 0, "Uploading…")
        sent = {"pct": 0}
        def cb(monitor: MultipartEncoderMonitor) -> None:
            if monitor.len:
                pct = int(monitor.bytes_read * 100 / monitor.len)
                if pct != sent["pct"]:
                    sent["pct"] = pct
                    asyncio.create_task(send_progress(session_id, item_id, "uploading", pct))
        monitor = MultipartEncoderMonitor(encoder, cb)
        headers = {"Accept": "application/json", "Content-Type": monitor.content_type, "x-immich-checksum": checksum, **immich_headers(request)}
        try:
            r = requests.post(f"{SETTINGS.normalized_base_url}/assets", headers=headers, data=monitor, timeout=120)
            if r.status_code in (200, 201):
                data = r.json()
                asset_id = data.get("id")
                db_insert_upload(checksum, file.filename, size, device_asset_id, asset_id, created_iso)
                status = data.get("status", "created")
                
                # Add to album if configured (invite overrides .env)
                if asset_id:
                    added = False
                    if invite_token:
                        # Only add if invite specified an album; do not fallback to env default
                        if target_album_id or target_album_name:
                            added = add_asset_to_album(asset_id, request=request, album_id_override=target_album_id, album_name_override=target_album_name)
                            if added:
                                status += f" (added to album '{target_album_name or target_album_id}')"
                    elif SETTINGS.album_name:
                        if add_asset_to_album(asset_id, request=request):
                            status += f" (added to album '{SETTINGS.album_name}')"

                await send_progress(session_id, item_id, "duplicate" if status == "duplicate" else "done", 100, status, asset_id)

                # Increment invite usage on success
                if invite_token:
                    try:
                        conn2 = sqlite3.connect(SETTINGS.state_db)
                        cur2 = conn2.cursor()
                        # Keep one-time used_count at 1; multi-use increments per asset
                        cur2.execute("SELECT max_uses FROM invites WHERE token = ?", (invite_token,))
                        row_mu = cur2.fetchone()
                        mx = None
                        try:
                            mx = int(row_mu[0]) if row_mu and row_mu[0] is not None else None
                        except Exception:
                            mx = None
                        if mx == 1:
                            cur2.execute("UPDATE invites SET used_count = 1 WHERE token = ?", (invite_token,))
                        else:
                            cur2.execute("UPDATE invites SET used_count = used_count + 1 WHERE token = ?", (invite_token,))
                        conn2.commit()
                        conn2.close()
                    except Exception as e:
                        logger.exception("Failed to increment invite usage: %s", e)
                # Log uploader identity and file metadata
                try:
                    connlg = sqlite3.connect(SETTINGS.state_db)
                    curlg = connlg.cursor()
                    curlg.execute(
                        """
                        CREATE TABLE IF NOT EXISTS upload_events (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            token TEXT,
                            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                            ip TEXT,
                            user_agent TEXT,
                            fingerprint TEXT,
                            filename TEXT,
                            size INTEGER,
                            checksum TEXT,
                            immich_asset_id TEXT
                        );
                        """
                    )
                    ip = None
                    try:
                        ip = (request.client.host if request and request.client else None) or request.headers.get('x-forwarded-for')
                    except Exception:
                        ip = None
                    ua = request.headers.get('user-agent', '') if request else ''
                    curlg.execute(
                        "INSERT INTO upload_events (token, ip, user_agent, fingerprint, filename, size, checksum, immich_asset_id) VALUES (?,?,?,?,?,?,?,?)",
                        (invite_token or '', ip, ua, fingerprint or '', file.filename, size, checksum, asset_id or None)
                    )
                    connlg.commit()
                    connlg.close()
                except Exception:
                    pass
                return JSONResponse({"id": asset_id, "status": status}, status_code=200)
            else:
                try:
                    msg = r.json().get("message", r.text)
                except Exception:
                    msg = r.text
                await send_progress(session_id, item_id, "error", 100, msg)
                return JSONResponse({"error": msg}, status_code=400)
        except Exception as e:
            await send_progress(session_id, item_id, "error", 100, str(e))
            return JSONResponse({"error": str(e)}, status_code=500)

    return await do_upload()

# --------- Chunked upload endpoints ---------

def _chunk_dir(session_id: str, item_id: str) -> str:
    safe_session = session_id.replace('/', '_')
    safe_item = item_id.replace('/', '_')
    return os.path.join(CHUNK_ROOT, safe_session, safe_item)

@app.post("/api/upload/chunk/init")
async def api_upload_chunk_init(request: Request) -> JSONResponse:
    """Initialize a chunked upload; creates a temp directory for incoming parts."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    item_id = (data or {}).get("item_id")
    session_id = (data or {}).get("session_id")
    if not item_id or not session_id:
        return JSONResponse({"error": "missing_ids"}, status_code=400)
    d = _chunk_dir(session_id, item_id)
    try:
        os.makedirs(d, exist_ok=True)
        # Write manifest for later use
        meta = {
            "name": (data or {}).get("name"),
            "size": (data or {}).get("size"),
            "last_modified": (data or {}).get("last_modified"),
            "invite_token": (data or {}).get("invite_token"),
            "content_type": (data or {}).get("content_type") or "application/octet-stream",
            "created_at": datetime.utcnow().isoformat(),
        }
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except Exception as e:
        logger.exception("Chunk init failed: %s", e)
        return JSONResponse({"error": "init_failed"}, status_code=500)
    return JSONResponse({"ok": True})

@app.post("/api/upload/chunk")
async def api_upload_chunk(
    request: Request,
    item_id: str = Form(...),
    session_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    invite_token: Optional[str] = Form(None),
    chunk: UploadFile = Form(...),
) -> JSONResponse:
    """Receive a single chunk; write to disk under chunk directory."""
    d = _chunk_dir(session_id, item_id)
    try:
        os.makedirs(d, exist_ok=True)
        # Persist invite token in meta if provided (for consistency)
        meta_path = os.path.join(d, "meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        else:
            meta = {}
        if invite_token:
            meta["invite_token"] = invite_token
        meta["total_chunks"] = int(total_chunks)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        # Save chunk
        content = await chunk.read()
        with open(os.path.join(d, f"part_{int(chunk_index):06d}"), "wb") as f:
            f.write(content)
    except Exception as e:
        logger.exception("Chunk write failed: %s", e)
        return JSONResponse({"error": "chunk_write_failed"}, status_code=500)
    return JSONResponse({"ok": True})

@app.post("/api/upload/chunk/complete")
async def api_upload_chunk_complete(request: Request) -> JSONResponse:
    """Assemble all parts and run the regular upload flow to Immich."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    item_id = (data or {}).get("item_id")
    session_id = (data or {}).get("session_id")
    name = (data or {}).get("name") or "upload.bin"
    last_modified = (data or {}).get("last_modified")
    invite_token = (data or {}).get("invite_token")
    fingerprint = (data or {}).get("fingerprint")
    content_type = (data or {}).get("content_type") or "application/octet-stream"
    if not item_id or not session_id:
        return JSONResponse({"error": "missing_ids"}, status_code=400)
    d = _chunk_dir(session_id, item_id)
    meta_path = os.path.join(d, "meta.json")
    # Basic validation
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    total_chunks = int(meta.get("total_chunks") or (data or {}).get("total_chunks") or 0)
    if total_chunks <= 0:
        return JSONResponse({"error": "missing_total"}, status_code=400)
    # Prefer the name captured at init if request did not include it
    if not name:
        try:
            name = meta.get("name") or name
        except Exception:
            pass
    if not name:
        name = "upload.bin"
    # Assemble
    parts = []
    try:
        for i in range(total_chunks):
            p = os.path.join(d, f"part_{i:06d}")
            if not os.path.exists(p):
                return JSONResponse({"error": "missing_part", "index": i}, status_code=400)
            with open(p, "rb") as f:
                parts.append(f.read())
        raw = b"".join(parts)
    except Exception as e:
        logger.exception("Assemble failed: %s", e)
        return JSONResponse({"error": "assemble_failed"}, status_code=500)
    # Cleanup parts promptly
    try:
        for i in range(total_chunks):
            try:
                os.remove(os.path.join(d, f"part_{i:06d}"))
            except Exception:
                pass
        try:
            os.remove(meta_path)
        except Exception:
            pass
        try:
            os.rmdir(d)
        except Exception:
            pass
    except Exception:
        pass

    # Now reuse the core logic from api_upload but with assembled bytes
    item_id_local = item_id
    session_id_local = session_id
    file_like_name = name
    file_size = len(raw)
    checksum = sha1_hex(raw)
    exif_created, exif_modified = read_exif_datetimes(raw)
    created_at = exif_created or (datetime.fromtimestamp(last_modified / 1000) if last_modified else datetime.utcnow())
    modified_at = exif_modified or created_at
    created_iso = created_at.isoformat()
    modified_iso = modified_at.isoformat()
    device_asset_id = f"{file_like_name}-{last_modified or 0}-{file_size}"

    # Local duplicate checks
    if db_lookup_checksum(checksum):
        await send_progress(session_id_local, item_id_local, "duplicate", 100, "Duplicate (by checksum - local cache)")
        return JSONResponse({"status": "duplicate", "id": None}, status_code=200)
    if db_lookup_device_asset(device_asset_id):
        await send_progress(session_id_local, item_id_local, "duplicate", 100, "Already uploaded from this device (local cache)")
        return JSONResponse({"status": "duplicate", "id": None}, status_code=200)

    await send_progress(session_id_local, item_id_local, "checking", 2, "Checking duplicates…")
    bulk = immich_bulk_check([{ "id": item_id_local, "checksum": checksum }])
    if bulk.get(item_id_local, {}).get("action") == "reject" and bulk[item_id_local].get("reason") == "duplicate":
        asset_id = bulk[item_id_local].get("assetId")
        db_insert_upload(checksum, file_like_name, file_size, device_asset_id, asset_id, created_iso)
        await send_progress(session_id_local, item_id_local, "duplicate", 100, "Duplicate (server)", asset_id)
        return JSONResponse({"status": "duplicate", "id": asset_id}, status_code=200)

    safe_name2 = sanitize_filename(file_like_name)
    def gen_encoder2() -> MultipartEncoder:
        return MultipartEncoder(fields={
            "assetData": (safe_name2, io.BytesIO(raw), content_type or "application/octet-stream"),
            "deviceAssetId": device_asset_id,
            "deviceId": f"python-{session_id_local}",
            "fileCreatedAt": created_iso,
            "fileModifiedAt": modified_iso,
            "isFavorite": "false",
            "filename": safe_name2,
            "originalFileName": safe_name2,
        })

    # Invite validation/gating mirrors api_upload
    target_album_id: Optional[str] = None
    target_album_name: Optional[str] = None
    if invite_token:
        try:
            conn = sqlite3.connect(SETTINGS.state_db)
            cur = conn.cursor()
            cur.execute("SELECT token, album_id, album_name, max_uses, used_count, expires_at, COALESCE(claimed,0), claimed_by_session, password_hash, COALESCE(disabled,0) FROM invites WHERE token = ?", (invite_token,))
            row = cur.fetchone()
            conn.close()
        except Exception as e:
            logger.exception("Invite lookup error: %s", e)
            row = None
        if not row:
            await send_progress(session_id_local, item_id_local, "error", 100, "Invalid invite token")
            return JSONResponse({"error": "invalid_invite"}, status_code=403)
        _, album_id, album_name, max_uses, used_count, expires_at, claimed, claimed_by_session, password_hash, disabled = row
        # Admin deactivation check
        try:
            if int(disabled) == 1:
                await send_progress(session_id_local, item_id_local, "error", 100, "Invite disabled")
                return JSONResponse({"error": "invite_disabled"}, status_code=403)
        except Exception:
            pass
        if password_hash:
            try:
                ia = request.session.get("inviteAuth") or {}
                if not ia.get(invite_token):
                    await send_progress(session_id_local, item_id_local, "error", 100, "Password required")
                    return JSONResponse({"error": "invite_password_required"}, status_code=403)
            except Exception:
                await send_progress(session_id_local, item_id_local, "error", 100, "Password required")
                return JSONResponse({"error": "invite_password_required"}, status_code=403)
        # expiry
        if expires_at:
            try:
                if datetime.utcnow() > datetime.fromisoformat(expires_at):
                    await send_progress(session_id_local, item_id_local, "error", 100, "Invite expired")
                    return JSONResponse({"error": "invite_expired"}, status_code=403)
            except Exception:
                pass
        try:
            max_uses_int = int(max_uses) if max_uses is not None else -1
        except Exception:
            max_uses_int = -1
        if max_uses_int == 1:
            if claimed:
                if claimed_by_session and claimed_by_session != session_id_local:
                    await send_progress(session_id_local, item_id_local, "error", 100, "Invite already used")
                    return JSONResponse({"error": "invite_claimed"}, status_code=403)
            else:
                try:
                    connc = sqlite3.connect(SETTINGS.state_db)
                    curc = connc.cursor()
                    curc.execute(
                        "UPDATE invites SET claimed = 1, claimed_at = CURRENT_TIMESTAMP, claimed_by_session = ? WHERE token = ? AND (claimed IS NULL OR claimed = 0)",
                        (session_id_local, invite_token)
                    )
                    connc.commit()
                    changed = connc.total_changes
                    connc.close()
                except Exception as e:
                    logger.exception("Invite claim failed: %s", e)
                    return JSONResponse({"error": "invite_claim_failed"}, status_code=500)
                if changed == 0:
                    try:
                        conn2 = sqlite3.connect(SETTINGS.state_db)
                        cur2 = conn2.cursor()
                        cur2.execute("SELECT claimed_by_session FROM invites WHERE token = ?", (invite_token,))
                        owner_row = cur2.fetchone()
                        conn2.close()
                        owner = owner_row[0] if owner_row else None
                    except Exception:
                        owner = None
                    if not owner or owner != session_id_local:
                        await send_progress(session_id_local, item_id_local, "error", 100, "Invite already used")
                        return JSONResponse({"error": "invite_claimed"}, status_code=403)
        else:
            if (used_count or 0) >= (max_uses_int if max_uses_int >= 0 else 10**9):
                await send_progress(session_id_local, item_id_local, "error", 100, "Invite already used up")
                return JSONResponse({"error": "invite_exhausted"}, status_code=403)
        target_album_id = album_id
        target_album_name = album_name

    await send_progress(session_id_local, item_id_local, "uploading", 0, "Uploading…")
    sent = {"pct": 0}
    def cb2(monitor: MultipartEncoderMonitor) -> None:
        if monitor.len:
            pct = int(monitor.bytes_read * 100 / monitor.len)
            if pct != sent["pct"]:
                sent["pct"] = pct
                asyncio.create_task(send_progress(session_id_local, item_id_local, "uploading", pct))
    encoder2 = gen_encoder2()
    monitor2 = MultipartEncoderMonitor(encoder2, cb2)
    headers = {"Accept": "application/json", "Content-Type": monitor2.content_type, "x-immich-checksum": checksum, **immich_headers(request)}
    try:
        r = requests.post(f"{SETTINGS.normalized_base_url}/assets", headers=headers, data=monitor2, timeout=120)
        if r.status_code in (200, 201):
            data_r = r.json()
            asset_id = data_r.get("id")
            db_insert_upload(checksum, file_like_name, file_size, device_asset_id, asset_id, created_iso)
            status = data_r.get("status", "created")
            if asset_id:
                added = False
                if invite_token:
                    # Only add if invite specified an album; do not fallback to env default
                    if target_album_id or target_album_name:
                        added = add_asset_to_album(asset_id, request=request, album_id_override=target_album_id, album_name_override=target_album_name)
                        if added:
                            status += f" (added to album '{target_album_name or target_album_id}')"
                elif SETTINGS.album_name:
                    if add_asset_to_album(asset_id, request=request):
                        status += f" (added to album '{SETTINGS.album_name}')"
            await send_progress(session_id_local, item_id_local, "duplicate" if status == "duplicate" else "done", 100, status, asset_id)
            if invite_token:
                try:
                    conn2 = sqlite3.connect(SETTINGS.state_db)
                    cur2 = conn2.cursor()
                    cur2.execute("SELECT max_uses FROM invites WHERE token = ?", (invite_token,))
                    row_mu = cur2.fetchone()
                    mx = None
                    try:
                        mx = int(row_mu[0]) if row_mu and row_mu[0] is not None else None
                    except Exception:
                        mx = None
                    if mx == 1:
                        cur2.execute("UPDATE invites SET used_count = 1 WHERE token = ?", (invite_token,))
                    else:
                        cur2.execute("UPDATE invites SET used_count = used_count + 1 WHERE token = ?", (invite_token,))
                    conn2.commit()
                    conn2.close()
                except Exception as e:
                    logger.exception("Failed to increment invite usage: %s", e)
            # Log uploader identity and file metadata
            try:
                connlg = sqlite3.connect(SETTINGS.state_db)
                curlg = connlg.cursor()
                curlg.execute(
                    """
                    CREATE TABLE IF NOT EXISTS upload_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token TEXT,
                        uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        ip TEXT,
                        user_agent TEXT,
                        fingerprint TEXT,
                        filename TEXT,
                        size INTEGER,
                        checksum TEXT,
                        immich_asset_id TEXT
                    );
                    """
                )
                ip = None
                try:
                    ip = (request.client.host if request and request.client else None) or request.headers.get('x-forwarded-for')
                except Exception:
                    ip = None
                ua = request.headers.get('user-agent', '') if request else ''
                curlg.execute(
                    "INSERT INTO upload_events (token, ip, user_agent, fingerprint, filename, size, checksum, immich_asset_id) VALUES (?,?,?,?,?,?,?,?)",
                    (invite_token or '', ip, ua, fingerprint or '', file_like_name, file_size, checksum, asset_id or None)
                )
                connlg.commit()
                connlg.close()
            except Exception:
                pass
            return JSONResponse({"id": asset_id, "status": status}, status_code=200)
        else:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            await send_progress(session_id_local, item_id_local, "error", 100, msg)
            return JSONResponse({"error": msg}, status_code=400)
    except Exception as e:
        await send_progress(session_id_local, item_id_local, "error", 100, str(e))
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/album/reset")
async def api_album_reset() -> dict:
    """Explicit trigger from the UI to clear cached album id."""
    reset_album_cache()
    return {"ok": True}

# ---------- Auth & Albums & Invites APIs ----------

@app.post("/api/login")
async def api_login(request: Request) -> JSONResponse:
    """Authenticate against Immich using email/password; store token in session."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    email = (body or {}).get("email")
    password = (body or {}).get("password")
    if not email or not password:
        return JSONResponse({"error": "missing_credentials"}, status_code=400)
    try:
        r = requests.post(f"{SETTINGS.normalized_base_url}/auth/login", headers={"Content-Type": "application/json", "Accept": "application/json"}, json={"email": email, "password": password}, timeout=15)
    except Exception as e:
        logger.exception("Login request failed: %s", e)
        return JSONResponse({"error": "login_failed"}, status_code=502)
    if r.status_code not in (200, 201):
        logger.warning("Auth rejected: %s - %s", r.status_code, r.text)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = r.json() if r.content else {}
    token = data.get("accessToken")
    if not token:
        logger.warning("Auth response missing accessToken")
        return JSONResponse({"error": "invalid_response"}, status_code=502)
    # Store only token and basic info in cookie session
    request.session.update({
        "accessToken": token,
        "userEmail": data.get("userEmail"),
        "userId": data.get("userId"),
        "name": data.get("name"),
        "isAdmin": data.get("isAdmin", False),
    })
    logger.info("User %s logged in", data.get("userEmail"))
    return JSONResponse({"ok": True, **{k: data.get(k) for k in ("userEmail","userId","name","isAdmin")}})

@app.post("/api/logout")
async def api_logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}

@app.get("/logout")
async def logout_get(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login")

@app.get("/api/albums")
async def api_albums(request: Request) -> JSONResponse:
    """Return list of albums if authorized; logs on 401/403."""
    try:
        r = requests.get(f"{SETTINGS.normalized_base_url}/albums", headers=immich_headers(request), timeout=10)
    except Exception as e:
        logger.exception("Albums request failed: %s", e)
        return JSONResponse({"error": "request_failed"}, status_code=502)
    if r.status_code == 200:
        return JSONResponse(r.json())
    if r.status_code in (401, 403):
        logger.warning("Album list not allowed: %s - %s", r.status_code, r.text)
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"error": "unexpected_status", "status": r.status_code}, status_code=502)

@app.post("/api/albums")
async def api_albums_create(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    name = (body or {}).get("name")
    if not name:
        return JSONResponse({"error": "missing_name"}, status_code=400)
    try:
        r = requests.post(f"{SETTINGS.normalized_base_url}/albums", headers={**immich_headers(request), "Content-Type": "application/json"}, json={"albumName": name}, timeout=10)
    except Exception as e:
        logger.exception("Create album failed: %s", e)
        return JSONResponse({"error": "request_failed"}, status_code=502)
    if r.status_code in (200, 201):
        return JSONResponse(r.json(), status_code=201)
    if r.status_code in (401, 403):
        logger.warning("Create album forbidden: %s - %s", r.status_code, r.text)
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"error": "unexpected_status", "status": r.status_code, "body": r.text}, status_code=502)

# ---------- Invites (one-time/expiring links) ----------

def ensure_invites_table() -> None:
    try:
        conn = sqlite3.connect(SETTINGS.state_db)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invites (
                token TEXT PRIMARY KEY,
                album_id TEXT,
                album_name TEXT,
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                expires_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Attempt to add new columns for claiming semantics
        try:
            cur.execute("ALTER TABLE invites ADD COLUMN claimed INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE invites ADD COLUMN claimed_at TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE invites ADD COLUMN claimed_by_session TEXT")
        except Exception:
            pass
        # Optional password protection for invites
        try:
            cur.execute("ALTER TABLE invites ADD COLUMN password_hash TEXT")
        except Exception:
            pass
        # Ownership and management fields (best-effort migrations)
        try:
            cur.execute("ALTER TABLE invites ADD COLUMN owner_user_id TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE invites ADD COLUMN owner_email TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE invites ADD COLUMN owner_name TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE invites ADD COLUMN name TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE invites ADD COLUMN disabled INTEGER DEFAULT 0")
        except Exception:
            pass
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Failed to ensure invites table: %s", e)

ensure_invites_table()

@app.post("/api/invites")
async def api_invites_create(request: Request) -> JSONResponse:
    """Create an invite link for uploads with optional expiry and max uses."""
    # Require a logged-in session to create invites
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    album_id = (body or {}).get("albumId")
    album_name = (body or {}).get("albumName")
    max_uses = (body or {}).get("maxUses", 1)
    invite_password = (body or {}).get("password")
    expires_days = (body or {}).get("expiresDays")
    # Normalize max_uses
    try:
        max_uses = int(max_uses)
    except Exception:
        max_uses = 1
    # Allow blank album for invites (no album association)
    if not album_name and SETTINGS.album_name and not album_id and album_name is not None:
        album_name = SETTINGS.album_name
    # If only album_name provided, resolve or create now to fix to an ID
    resolved_album_id = None
    if not album_id and album_name:
        resolved_album_id = get_or_create_album(request=request, album_name_override=album_name)
    else:
        resolved_album_id = album_id
    # Compute expiry
    expires_at = None
    if expires_days is not None:
        try:
            days = int(expires_days)
            expires_at = (datetime.utcnow()).replace(microsecond=0).isoformat()
            # Use timedelta
            from datetime import timedelta
            expires_at = (datetime.utcnow() + timedelta(days=days)).replace(microsecond=0).isoformat()
        except Exception:
            expires_at = None
    # Generate token
    import uuid
    token = uuid.uuid4().hex
    # Prepare password hash, if provided
    def hash_password(pw: str) -> str:
        try:
            if not pw:
                return ""
            import os as _os
            import binascii as _binascii
            salt = _os.urandom(16)
            iterations = 200_000
            dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, iterations)
            return f"pbkdf2_sha256${iterations}${_binascii.hexlify(salt).decode()}${_binascii.hexlify(dk).decode()}"
        except Exception:
            return ""
    pw_hash = hash_password(invite_password or "") if (invite_password and str(invite_password).strip()) else None
    # Owner info from session
    owner_user_id = str(request.session.get("userId") or "")
    owner_email = str(request.session.get("userEmail") or "")
    owner_name = str(request.session.get("name") or "")
    # Friendly name: default to album + creation timestamp if not provided in future updates
    # Here we set a default immediately
    now_tag = datetime.utcnow().strftime("%Y%m%d-%H%M")
    default_link_name = f"{album_name or 'NoAlbum'}-{now_tag}"
    try:
        conn = sqlite3.connect(SETTINGS.state_db)
        cur = conn.cursor()
        if pw_hash:
            cur.execute(
                "INSERT INTO invites (token, album_id, album_name, max_uses, expires_at, password_hash, owner_user_id, owner_email, owner_name, name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (token, resolved_album_id, album_name, max_uses, expires_at, pw_hash, owner_user_id, owner_email, owner_name, default_link_name)
            )
        else:
            cur.execute(
                "INSERT INTO invites (token, album_id, album_name, max_uses, expires_at, owner_user_id, owner_email, owner_name, name) VALUES (?,?,?,?,?,?,?,?,?)",
                (token, resolved_album_id, album_name, max_uses, expires_at, owner_user_id, owner_email, owner_name, default_link_name)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Failed to create invite: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    # Build absolute URL using PUBLIC_BASE_URL if set, else request base
    try:
        base_url = SETTINGS.public_base_url.strip().rstrip('/') if SETTINGS.public_base_url else str(request.base_url).rstrip('/')
    except Exception:
        base_url = str(request.base_url).rstrip('/')
    absolute = f"{base_url}/invite/{token}"
    return JSONResponse({
        "ok": True,
        "token": token,
        "url": f"/invite/{token}",
        "absoluteUrl": absolute,
        "albumId": resolved_album_id,
        "albumName": album_name,
        "maxUses": max_uses,
        "expiresAt": expires_at,
        "name": default_link_name
    })

@app.get("/api/invites")
async def api_invites_list(request: Request) -> JSONResponse:
    """List invites owned by the logged-in user, with optional q/sort filters."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    owner_user_id = str(request.session.get("userId") or "")
    q = (request.query_params.get("q") or "").strip()
    sort = (request.query_params.get("sort") or "-created").strip()
    # Map sort tokens to SQL
    sort_sql = "created_at DESC"
    if sort in ("created", "+created"):
        sort_sql = "created_at ASC"
    elif sort in ("-created",):
        sort_sql = "created_at DESC"
    elif sort in ("expires", "+expires"):
        sort_sql = "expires_at ASC"
    elif sort in ("-expires",):
        sort_sql = "expires_at DESC"
    elif sort in ("name", "+name"):
        sort_sql = "name ASC"
    elif sort in ("-name",):
        sort_sql = "name DESC"
    try:
        conn = sqlite3.connect(SETTINGS.state_db)
        cur = conn.cursor()
        if q:
            like = f"%{q}%"
            cur.execute(
                """
                SELECT token, name, album_id, album_name, max_uses, used_count, expires_at, COALESCE(claimed,0), COALESCE(disabled,0), created_at
                FROM invites
                WHERE owner_user_id = ? AND (
                    COALESCE(name,'') LIKE ? OR COALESCE(album_name,'') LIKE ? OR token LIKE ?
                )
                ORDER BY """ + sort_sql,
                (owner_user_id, like, like, like)
            )
        else:
            cur.execute(
                f"SELECT token, name, album_id, album_name, max_uses, used_count, expires_at, COALESCE(claimed,0), COALESCE(disabled,0), created_at FROM invites WHERE owner_user_id = ? ORDER BY {sort_sql}",
                (owner_user_id,)
            )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.exception("List invites failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    items = []
    now = datetime.utcnow()
    for (token, name, album_id, album_name, max_uses, used_count, expires_at, claimed, disabled, created_at) in rows:
        try:
            max_uses_int = int(max_uses) if max_uses is not None else -1
        except Exception:
            max_uses_int = -1
        remaining = None
        try:
            if max_uses_int >= 0:
                remaining = int(max_uses_int) - int(used_count or 0)
        except Exception:
            remaining = None
        expired = False
        if expires_at:
            try:
                expired = now > datetime.fromisoformat(expires_at)
            except Exception:
                expired = False
        inactive_reason = None
        active = True
        if (max_uses_int == 1 and claimed) or (remaining is not None and remaining <= 0):
            active = False
            inactive_reason = "claimed" if max_uses_int == 1 else "exhausted"
        if expired:
            active = False
            inactive_reason = inactive_reason or "expired"
        try:
            if int(disabled) == 1:
                active = False
                inactive_reason = "disabled"
        except Exception:
            pass
        items.append({
            "token": token,
            "name": name,
            "albumId": album_id,
            "albumName": album_name,
            "maxUses": max_uses,
            "used": used_count or 0,
            "remaining": remaining,
            "expiresAt": expires_at,
            "active": active,
            "inactiveReason": inactive_reason,
            "createdAt": created_at,
        })
    return JSONResponse({"items": items})

@app.patch("/api/invite/{token}")
async def api_invite_update(token: str, request: Request) -> JSONResponse:
    """Update invite fields: name, disabled, maxUses, expiresAt/expiresDays, password, resetUsage."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    owner_user_id = str(request.session.get("userId") or "")
    # Build dynamic update
    fields = []
    params = []
    # Name
    if "name" in (body or {}):
        fields.append("name = ?")
        params.append(str((body or {}).get("name") or "").strip())
    # Disabled toggle
    if "disabled" in (body or {}):
        try:
            disabled = 1 if (bool((body or {}).get("disabled")) is True) else 0
        except Exception:
            disabled = 0
        fields.append("disabled = ?")
        params.append(disabled)
    # Max uses
    if "maxUses" in (body or {}):
        try:
            mu = int((body or {}).get("maxUses"))
        except Exception:
            mu = 1
        fields.append("max_uses = ?")
        params.append(mu)
    # Expiration
    if "expiresAt" in (body or {}) or "expiresDays" in (body or {}):
        expires_at = None
        if (body or {}).get("expiresAt"):
            try:
                # trust provided ISO string
                expires_at = str((body or {}).get("expiresAt"))
            except Exception:
                expires_at = None
        else:
            try:
                days = int((body or {}).get("expiresDays"))
                from datetime import timedelta
                expires_at = (datetime.utcnow() + timedelta(days=days)).replace(microsecond=0).isoformat()
            except Exception:
                expires_at = None
        fields.append("expires_at = ?")
        params.append(expires_at)
    # Password
    if "password" in (body or {}):
        pw = str((body or {}).get("password") or "").strip()
        if pw:
            # Reuse hasher from above
            def _hash_pw(pw: str) -> str:
                import os as _os
                import binascii as _binascii
                salt = _os.urandom(16)
                iterations = 200_000
                dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, iterations)
                return f"pbkdf2_sha256${iterations}${_binascii.hexlify(salt).decode()}${_binascii.hexlify(dk).decode()}"
            fields.append("password_hash = ?")
            params.append(_hash_pw(pw))
        else:
            fields.append("password_hash = NULL")
    # Reset usage
    reset_usage = bool((body or {}).get("resetUsage"))
    try:
        if fields:
            conn = sqlite3.connect(SETTINGS.state_db)
            cur = conn.cursor()
            cur.execute(
                f"UPDATE invites SET {', '.join(fields)} WHERE token = ? AND owner_user_id = ?",
                (*params, token, owner_user_id)
            )
            if reset_usage:
                cur.execute("UPDATE invites SET used_count = 0, claimed = 0, claimed_at = NULL, claimed_by_session = NULL WHERE token = ? AND owner_user_id = ?", (token, owner_user_id))
            conn.commit()
            updated = conn.total_changes
            conn.close()
        else:
            updated = 0
    except Exception as e:
        logger.exception("Invite update failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    if updated == 0:
        return JSONResponse({"ok": False, "updated": 0}, status_code=404)
    return JSONResponse({"ok": True, "updated": updated})

@app.post("/api/invites/bulk")
async def api_invites_bulk(request: Request) -> JSONResponse:
    """Bulk enable/disable invites owned by current user. Body: {tokens:[], action:'disable'|'enable'}"""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    tokens = list((body or {}).get("tokens") or [])
    action = str((body or {}).get("action") or "disable").lower().strip()
    if not tokens:
        return JSONResponse({"error": "missing_tokens"}, status_code=400)
    val = 1 if action == "disable" else 0
    owner_user_id = str(request.session.get("userId") or "")
    try:
        conn = sqlite3.connect(SETTINGS.state_db)
        cur = conn.cursor()
        # Build query with correct number of placeholders
        placeholders = ",".join(["?"] * len(tokens))
        cur.execute(
            f"UPDATE invites SET disabled = ? WHERE owner_user_id = ? AND token IN ({placeholders})",
            (val, owner_user_id, *tokens)
        )
        conn.commit()
        changed = conn.total_changes
        conn.close()
    except Exception as e:
        logger.exception("Bulk update failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    return JSONResponse({"ok": True, "updated": changed})

@app.post("/api/invites/delete")
async def api_invites_delete(request: Request) -> JSONResponse:
    """Hard delete invites owned by the current user and their upload logs.

    Body: { tokens: ["...", ...] }
    """
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    tokens = list((body or {}).get("tokens") or [])
    if not tokens:
        return JSONResponse({"error": "missing_tokens"}, status_code=400)
    owner_user_id = str(request.session.get("userId") or "")
    try:
        conn = sqlite3.connect(SETTINGS.state_db)
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(tokens))
        # Delete upload events first to avoid orphan rows
        cur.execute(
            f"DELETE FROM upload_events WHERE token IN ({placeholders})",
            (*tokens,)
        )
        # Delete invites scoped to owner
        cur.execute(
            f"DELETE FROM invites WHERE owner_user_id = ? AND token IN ({placeholders})",
            (owner_user_id, *tokens)
        )
        conn.commit()
        changed = conn.total_changes
        conn.close()
    except Exception as e:
        logger.exception("Bulk delete failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    return JSONResponse({"ok": True, "deleted": changed})

@app.get("/api/invite/{token}/uploads")
async def api_invite_uploads(token: str, request: Request) -> JSONResponse:
    """Return upload events for a given token (owner-only)."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    owner_user_id = str(request.session.get("userId") or "")
    try:
        conn = sqlite3.connect(SETTINGS.state_db)
        cur = conn.cursor()
        # Verify ownership
        cur.execute("SELECT 1 FROM invites WHERE token = ? AND owner_user_id = ?", (token, owner_user_id))
        row = cur.fetchone()
        if not row:
            conn.close()
            return JSONResponse({"error": "forbidden"}, status_code=403)
        cur.execute("SELECT uploaded_at, ip, user_agent, fingerprint, filename, size, checksum, immich_asset_id FROM upload_events WHERE token = ? ORDER BY uploaded_at DESC LIMIT 500", (token,))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.exception("Fetch uploads failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    items = []
    for uploaded_at, ip, ua, fp, filename, size, checksum, asset_id in rows:
        items.append({
            "uploadedAt": uploaded_at,
            "ip": ip,
            "userAgent": ua,
            "fingerprint": fp,
            "filename": filename,
            "size": size,
            "checksum": checksum,
            "assetId": asset_id,
        })
    return JSONResponse({"items": items})

@app.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(token: str, request: Request) -> HTMLResponse:
    # If public invites disabled and no user session, require login
    #if  not request.session.get("accessToken"):
    #    return RedirectResponse(url="/login")
    return FileResponse(os.path.join(FRONTEND_DIR, "invite.html"))

@app.get("/api/invite/{token}")
async def api_invite_info(token: str, request: Request) -> JSONResponse:
    try:
        conn = sqlite3.connect(SETTINGS.state_db)
        cur = conn.cursor()
        cur.execute("SELECT token, album_id, album_name, max_uses, used_count, expires_at, COALESCE(claimed,0), claimed_at, password_hash, COALESCE(disabled,0), name FROM invites WHERE token = ?", (token,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.exception("Invite info error: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    if not row:
        return JSONResponse({"error": "not_found"}, status_code=404)
    _, album_id, album_name, max_uses, used_count, expires_at, claimed, claimed_at, password_hash, disabled, link_name = row
    # compute remaining
    remaining = None
    try:
        if max_uses is not None and int(max_uses) >= 0:
            remaining = int(max_uses) - int(used_count or 0)
    except Exception:
        remaining = None
    # compute state flags
    try:
        one_time = (int(max_uses) == 1)
    except Exception:
        one_time = False
    expired = False
    if expires_at:
        try:
            expired = datetime.utcnow() > datetime.fromisoformat(expires_at)
        except Exception:
            expired = False
    deactivated = False
    reason = None
    if one_time and claimed:
        deactivated = True
        reason = "claimed"
    elif remaining is not None and remaining <= 0:
        deactivated = True
        reason = "exhausted"
    if expired:
        deactivated = True
        reason = reason or "expired"
    # Admin disabled flag
    try:
        if int(disabled) == 1:
            deactivated = True
            reason = "disabled"
    except Exception:
        pass
    active = not deactivated
    # Password requirement + authorization state
    password_required = bool(password_hash)
    authorized = False
    try:
        ia = request.session.get("inviteAuth") or {}
        authorized = bool(ia.get(token))
    except Exception:
        authorized = False
    return JSONResponse({
        "token": token,
        "albumId": album_id,
        "albumName": album_name,
        "name": link_name,
        "maxUses": max_uses,
        "used": used_count or 0,
        "remaining": remaining,
        "expiresAt": expires_at,
        "oneTime": one_time,
        "claimed": bool(claimed),
        "claimedAt": claimed_at,
        "expired": expired,
        "active": active,
        "inactiveReason": (None if active else (reason or "inactive")),
        "passwordRequired": password_required,
        "authorized": authorized,
    })

@app.post("/api/invite/{token}/auth")
async def api_invite_auth(token: str, request: Request) -> JSONResponse:
    """Validate a password for an invite token, and mark this session authorized if valid."""
    try:
        body = await request.json()
    except Exception:
        body = None
    provided = (body or {}).get("password") if isinstance(body, dict) else None
    try:
        conn = sqlite3.connect(SETTINGS.state_db)
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM invites WHERE token = ?", (token,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.exception("Invite auth lookup error: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    if not row:
        return JSONResponse({"error": "not_found"}, status_code=404)
    password_hash = row[0]
    if not password_hash:
        # No password required; mark as authorized to simplify client flow
        ia = request.session.get("inviteAuth") or {}
        ia[token] = True
        request.session["inviteAuth"] = ia
        return JSONResponse({"ok": True, "authorized": True})
    # verify
    def verify_password(stored: str, pw: Optional[str]) -> bool:
        if not pw:
            return False
        try:
            algo, iter_s, salt_hex, hash_hex = stored.split("$")
            if algo != 'pbkdf2_sha256':
                return False
            iterations = int(iter_s)
            import binascii as _binascii
            salt = _binascii.unhexlify(salt_hex)
            dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, iterations)
            return _binascii.hexlify(dk).decode() == hash_hex
        except Exception:
            return False
    if not verify_password(password_hash, provided):
        return JSONResponse({"error": "invalid_password"}, status_code=403)
    ia = request.session.get("inviteAuth") or {}
    ia[token] = True
    request.session["inviteAuth"] = ia
    return JSONResponse({"ok": True, "authorized": True})

@app.get("/api/qr", response_model=None)
async def api_qr(request: Request):
    """Generate a QR code PNG for a given text (query param 'text')."""
    text = request.query_params.get("text")
    if not text:
        return JSONResponse({"error": "missing_text"}, status_code=400)
    if qrcode is None:
        logger.warning("qrcode library not installed; cannot generate QR")
        return JSONResponse({"error": "qr_not_available"}, status_code=501)
    import io as _io
    img = qrcode.make(text)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")

"""
Note: Do not run this module directly. Use `python main.py` from
project root, which starts `uvicorn app.app:app` with reload.
"""
