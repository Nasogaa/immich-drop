"""Pytest fixtures for invite-gating regression tests.

Sets up an isolated temporary state DB and chunk directory before importing the
app so the module-level ``db_init()`` / ``ensure_invites_table()`` calls target a
throwaway location, then provides a TestClient plus helpers to seed invites and
spy on the network / hashing paths that must NOT run on a rejected preflight.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

# Configure environment BEFORE importing the app so module-load side effects
# (db_init/ensure_invites_table) use a temp DB and a deterministic secret.
_TMP_DIR = tempfile.mkdtemp(prefix="invite_gating_tests_")
os.environ["STATE_DB"] = os.path.join(_TMP_DIR, "state.db")
os.environ["SESSION_SECRET"] = "test-secret-fixed"
os.environ["PUBLIC_UPLOAD_PAGE_ENABLED"] = "false"
os.environ.setdefault("IMMICH_BASE_URL", "http://immich.invalid/api")

from fastapi.testclient import TestClient  # noqa: E402

from app import app as appmod  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path):
    """Point SETTINGS at a fresh DB + chunk root for each test and (re)create tables."""
    db_path = str(tmp_path / "state.db")
    chunk_root = str(tmp_path / "chunks")
    os.makedirs(chunk_root, exist_ok=True)

    appmod.SETTINGS.state_db = db_path
    appmod.CHUNK_ROOT = chunk_root
    # Default to the hardened (public disabled) posture; individual tests flip it.
    appmod.SETTINGS.public_upload_page_enabled = False

    appmod.db_init()
    appmod.ensure_invites_table()
    return {"db": db_path, "chunk_root": chunk_root}


@pytest.fixture()
def client(fresh_db):
    return TestClient(appmod.app)


@pytest.fixture()
def spies(monkeypatch):
    """Spy on the file-processing / network calls that must not run on rejection.

    - ``sha1_hex`` is the first thing invoked after the uploaded file is read into
      memory, so a zero call-count proves no file body was processed.
    - ``requests.post`` is the outbound Immich upload; it must never fire on reject.
    """
    calls = {"sha1": 0, "post": 0}

    real_sha1 = appmod.sha1_hex

    def sha1_spy(data):
        calls["sha1"] += 1
        return real_sha1(data)

    def post_spy(*args, **kwargs):  # pragma: no cover - should not run on reject
        calls["post"] += 1

        class _Resp:
            status_code = 201

            def json(self):
                return {"id": "asset-xyz", "status": "created"}

        return _Resp()

    monkeypatch.setattr(appmod, "sha1_hex", sha1_spy)
    monkeypatch.setattr(appmod.requests, "post", post_spy)
    # Never reach a real Immich for bulk-check either.
    monkeypatch.setattr(appmod, "immich_bulk_check", lambda checks: {})
    return calls


# ---------- helpers ----------

def seed_invite(db_path, token, *, max_uses=-1, expires_at=None,
                password_hash=None, disabled=0, used_count=0, claimed=0):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO invites (token, album_id, album_name, max_uses, used_count, "
        "expires_at, password_hash, disabled, claimed) VALUES (?,?,?,?,?,?,?,?,?)",
        (token, None, None, max_uses, used_count, expires_at, password_hash, disabled, claimed),
    )
    conn.commit()
    conn.close()


def invite_row(db_path, token):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT used_count, COALESCE(claimed,0) FROM invites WHERE token = ?",
        (token,),
    )
    row = cur.fetchone()
    conn.close()
    return {"used_count": row[0], "claimed": row[1]} if row else None


def past_iso():
    return (datetime.utcnow() - timedelta(days=1)).isoformat()


def future_iso():
    return (datetime.utcnow() + timedelta(days=1)).isoformat()
