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
from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from PIL import Image, ExifTags
from dotenv import load_dotenv

from app.config import Settings, load_settings

# ---- Load environment / defaults ----
load_dotenv()
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8080"))
STATE_DB = os.getenv("STATE_DB", "./state.db")
USER_CONFIG_FILE = os.getenv("USER_CONFIG_FILE", "./users.json")
users_cache = None

# ---- App & static ----
app = FastAPI(title="Immich Drop Uploader (Python)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Global settings (read-only at runtime)
SETTINGS: Settings = load_settings()

# Album cache
ALBUM_ID: Optional[str] = None


def load_users_config():
    """Carrega configuração dos usuários do arquivo JSON."""
    global users_cache
    if users_cache is None:
        try:
            with open(USER_CONFIG_FILE, 'r', encoding='utf-8') as f:
                users_cache = json.load(f)
        except Exception as e:
            print(f"Erro ao carregar users.json: {e}")
            users_cache = {"users": []}
    return users_cache

def get_user_api_key(user_id: str) -> Optional[str]:
    """Retorna a API key para um usuário específico."""
    config = load_users_config()
    for user in config.get("users", []):
        if user["id"] == user_id:
            return user["api_key"]
    return None

def immich_headers_for_user(user_id: str) -> dict:
    """Headers para chamadas da API do Immich para um usuário específico."""
    api_key = get_user_api_key(user_id) or SETTINGS.immich_api_key
    return {"Accept": "application/json", "x-api-key": api_key}

# ---------- DB (local dedupe cache) ----------

def db_init() -> None:
    """Create the local SQLite table used for duplicate checks (idempotent)."""
    conn = sqlite3.connect(STATE_DB)
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
    conn.commit()
    conn.close()

def db_lookup_checksum(checksum: str) -> Optional[dict]:
    """Return a record for the given checksum if seen before (None if not)."""
    conn = sqlite3.connect(STATE_DB)
    cur = conn.cursor()
    cur.execute("SELECT checksum, immich_asset_id FROM uploads WHERE checksum = ?", (checksum,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"checksum": row[0], "immich_asset_id": row[1]}
    return None

def db_lookup_device_asset(device_asset_id: str) -> bool:
    """True if a deviceAssetId has been uploaded by this service previously."""
    conn = sqlite3.connect(STATE_DB)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM uploads WHERE device_asset_id = ?", (device_asset_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row)

def db_insert_upload(checksum: str, filename: str, size: int, device_asset_id: str, immich_asset_id: Optional[str], created_at: str) -> None:
    """Insert a newly-uploaded asset into the local cache (ignore on duplicates)."""
    conn = sqlite3.connect(STATE_DB)
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

def immich_headers() -> dict:
    """Headers for Immich API calls (keeps key server-side)."""
    return {"Accept": "application/json", "x-api-key": SETTINGS.immich_api_key}

def get_or_create_album() -> Optional[str]:
    """Get existing album by name or create a new one. Returns album ID or None."""
    global ALBUM_ID
    
    # Skip if no album name configured
    if not SETTINGS.album_name:
        return None
    
    # Return cached album ID if already fetched
    if ALBUM_ID:
        return ALBUM_ID
    
    try:
        # First, try to find existing album
        url = f"{SETTINGS.normalized_base_url}/albums"
        r = requests.get(url, headers=immich_headers(), timeout=10)
        
        if r.status_code == 200:
            albums = r.json()
            for album in albums:
                if album.get("albumName") == SETTINGS.album_name:
                    ALBUM_ID = album.get("id")
                    print(f"Found existing album '{SETTINGS.album_name}' with ID: {ALBUM_ID}")
                    return ALBUM_ID
        
        # Album doesn't exist, create it
        create_url = f"{SETTINGS.normalized_base_url}/albums"
        payload = {
            "albumName": SETTINGS.album_name,
            "description": "Auto-created album for Immich Drop uploads"
        }
        r = requests.post(create_url, headers={**immich_headers(), "Content-Type": "application/json"}, 
                         json=payload, timeout=10)
        
        if r.status_code in (200, 201):
            data = r.json()
            ALBUM_ID = data.get("id")
            print(f"Created new album '{SETTINGS.album_name}' with ID: {ALBUM_ID}")
            return ALBUM_ID
        else:
            print(f"Failed to create album: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"Error managing album: {e}")
    
    return None

def add_asset_to_album(asset_id: str) -> bool:
    """Add an asset to the configured album. Returns True on success."""
    album_id = get_or_create_album()
    if not album_id or not asset_id:
        return False
    
    try:
        url = f"{SETTINGS.normalized_base_url}/albums/{album_id}/assets"
        payload = {"ids": [asset_id]}
        r = requests.put(url, headers={**immich_headers(), "Content-Type": "application/json"}, 
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
        print(f"Error adding asset to album: {e}")
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

def add_asset_to_specific_album(asset_id: str, album_id: str, user_id: str) -> bool:
    """Adiciona um asset a um álbum específico."""
    if not album_id or not asset_id:
        return False
    
    try:
        url = f"{SETTINGS.normalized_base_url}/albums/{album_id}/assets"
        payload = {"ids": [asset_id]}
        r = requests.put(
            url, 
            headers={**immich_headers_for_user(user_id), "Content-Type": "application/json"}, 
            json=payload, 
            timeout=10
        )
        
        if r.status_code == 200:
            results = r.json()
            for result in results:
                if result.get("success") or result.get("error") == "duplicate":
                    return True
        return False
    except Exception as e:
        print(f"Erro ao adicionar asset ao álbum: {e}")
        return False
        
# ---------- Routes ----------

@app.get("/", response_class=HTMLResponse)
async def index(_: Request) -> HTMLResponse:
    """Serve the SPA (frontend/index.html)."""
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

@app.post("/api/ping")
async def api_ping() -> dict:
    """Connectivity test endpoint used by the UI to display a temporary banner."""
    return {
        "ok": immich_ping(), 
        "base_url": SETTINGS.normalized_base_url,
        "album_name": SETTINGS.album_name if SETTINGS.album_name else None
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
    _: Request,
    file: UploadFile,
    item_id: str = Form(...),
    session_id: str = Form(...),
    last_modified: Optional[int] = Form(None),
    user_id: Optional[str] = Form(None),  # NOVO
    album_id: Optional[str] = Form(None),  # NOVO
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

    def gen_encoder() -> MultipartEncoder:
        return MultipartEncoder(fields={
            "assetData": (file.filename, io.BytesIO(raw), file.content_type or "application/octet-stream"),
            "deviceAssetId": device_asset_id,
            "deviceId": f"python-{session_id}",
            "fileCreatedAt": created_iso,
            "fileModifiedAt": modified_iso,
            "isFavorite": "false",
            "filename": file.filename,
        })

    encoder = gen_encoder()

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
        #headers = {"Accept": "application/json", "Content-Type": monitor.content_type, "x-immich-checksum": checksum, **immich_headers()}
        api_headers = immich_headers_for_user(user_id) if user_id else immich_headers()
        headers = {"Accept": "application/json", "Content-Type": monitor.content_type, "x-immich-checksum": checksum, **api_headers}
        try:
            r = requests.post(f"{SETTINGS.normalized_base_url}/assets", headers=headers, data=monitor, timeout=120)
            if r.status_code in (200, 201):
                data = r.json()
                asset_id = data.get("id")
                db_insert_upload(checksum, file.filename, size, device_asset_id, asset_id, created_iso)
                status = data.get("status", "created")
                
                # Add to album if configured
                # if SETTINGS.album_name and asset_id:
                #     if add_asset_to_album(asset_id):
                #         status += f" (added to album '{SETTINGS.album_name}')"
                
                # Add to album if specified
                if album_id and asset_id:
                    if add_asset_to_specific_album(asset_id, album_id, user_id):
                        status += f" (adicionado ao álbum)"
                elif SETTINGS.album_name and asset_id:
                    if add_asset_to_album(asset_id):
                        status += f" (added to album '{SETTINGS.album_name}')"

                await send_progress(session_id, item_id, "duplicate" if status == "duplicate" else "done", 100, status, asset_id)
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

@app.get("/api/users")
async def api_users() -> dict:
    """Retorna lista de usuários disponíveis."""
    config = load_users_config()
    users = []
    for user in config.get("users", []):
        users.append({
            "id": user["id"],
            "name": user["name"],
            "email": user["email"]
        })
    return {"users": users}

@app.get("/api/albums")
async def api_albums(userId: Optional[str] = None) -> dict:
    """Retorna álbuns do usuário especificado."""
    if not userId:
        return {"error": "userId é obrigatório"}, 400
    
    try:
        url = f"{SETTINGS.normalized_base_url}/albums"
        r = requests.get(url, headers=immich_headers_for_user(userId), timeout=10)
        
        if r.status_code == 200:
            albums = r.json()
            return {"albums": albums}
        else:
            return {"error": f"Erro ao buscar álbuns: {r.status_code}"}, 400
    except Exception as e:
        return {"error": f"Erro ao conectar com Immich: {str(e)}"}, 500

@app.post("/api/albums")
async def api_create_album(request: Request) -> dict:
    """Cria um novo álbum para o usuário especificado."""
    try:
        data = await request.json()
        user_id = data.get("userId")
        album_name = data.get("albumName")
        
        if not user_id or not album_name:
            return JSONResponse({"error": "userId e albumName são obrigatórios"}, status_code=400)
        
        url = f"{SETTINGS.normalized_base_url}/albums"
        payload = {
            "albumName": album_name,
            "description": f"Criado via Immich Drop para {user_id}"
        }
        
        r = requests.post(
            url, 
            headers={**immich_headers_for_user(user_id), "Content-Type": "application/json"}, 
            json=payload, 
            timeout=10
        )
        
        if r.status_code in (200, 201):
            album = r.json()
            return {"album": album}
        else:
            error_msg = r.text
            try:
                error_data = r.json()
                error_msg = error_data.get("message", error_msg)
            except:
                pass
            return JSONResponse({"error": f"Erro ao criar álbum: {error_msg}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Erro interno: {str(e)}"}, status_code=500)

"""
Note: Do not run this module directly. Use `python main.py` from
project root, which starts `uvicorn app.app:app` with reload.
"""
