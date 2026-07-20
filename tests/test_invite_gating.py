"""Regression tests for invite-gated uploads (public deployment hardening).

When PUBLIC_UPLOAD_PAGE_ENABLED is false, the upload API must reject requests
that lack a valid, active invite *before* reading/assembling/writing any file
data. Valid tokens and the public-enabled tokenless mode keep working, and the
preflight must not claim invites or bump usage.
"""
from __future__ import annotations

import io
import json
import os

from conftest import (
    seed_invite,
    invite_row,
    past_iso,
    future_iso,
)


def _upload(client, *, token=None, extra=None, filename="pic.jpg"):
    data = {"item_id": "item-1", "session_id": "sess-1", "fingerprint": "fp"}
    if token is not None:
        data["invite_token"] = token
    if extra:
        data.update(extra)
    files = {"file": (filename, io.BytesIO(b"hello-bytes"), "image/jpeg")}
    return client.post("/api/upload", data=data, files=files)


def _chunk_dir(chunk_root, session_id="sess-1", item_id="item-1"):
    return os.path.join(chunk_root, session_id, item_id)


# --------- /api/upload preflight rejections (public disabled) ---------

def test_upload_rejects_missing_token(client, spies):
    r = _upload(client, token=None)
    assert r.status_code == 403
    assert spies["sha1"] == 0
    assert spies["post"] == 0


def test_upload_rejects_empty_token(client, spies):
    r = _upload(client, token="")
    assert r.status_code == 403
    assert spies["sha1"] == 0
    assert spies["post"] == 0


def test_upload_rejects_invalid_token(client, spies):
    r = _upload(client, token="does-not-exist")
    assert r.status_code == 403
    assert r.json().get("error") == "invalid_invite"
    assert spies["sha1"] == 0
    assert spies["post"] == 0


def test_upload_rejects_disabled_invite(client, fresh_db, spies):
    seed_invite(fresh_db["db"], "tok-disabled", disabled=1)
    r = _upload(client, token="tok-disabled")
    assert r.status_code == 403
    assert r.json().get("error") == "invite_disabled"
    assert spies["sha1"] == 0
    assert spies["post"] == 0


def test_upload_rejects_expired_invite(client, fresh_db, spies):
    seed_invite(fresh_db["db"], "tok-expired", expires_at=past_iso())
    r = _upload(client, token="tok-expired")
    assert r.status_code == 403
    assert r.json().get("error") == "invite_expired"
    assert spies["sha1"] == 0
    assert spies["post"] == 0


def test_upload_rejects_password_protected_unauthorized(client, fresh_db, spies):
    seed_invite(fresh_db["db"], "tok-pw", password_hash="somehash")
    r = _upload(client, token="tok-pw")
    assert r.status_code == 403
    assert r.json().get("error") == "invite_password_required"
    assert spies["sha1"] == 0
    assert spies["post"] == 0


def test_upload_preflight_does_not_claim_or_increment(client, fresh_db, spies):
    # A one-time, password-protected invite that gets rejected must remain pristine.
    seed_invite(fresh_db["db"], "tok-pw1", max_uses=1, password_hash="somehash")
    r = _upload(client, token="tok-pw1")
    assert r.status_code == 403
    row = invite_row(fresh_db["db"], "tok-pw1")
    assert row == {"used_count": 0, "claimed": 0}


# --------- /api/upload happy paths ---------

def test_upload_valid_token_proceeds_and_increments(client, fresh_db, spies):
    seed_invite(fresh_db["db"], "tok-ok", max_uses=-1)
    r = _upload(client, token="tok-ok")
    assert r.status_code == 200
    assert spies["sha1"] >= 1
    assert spies["post"] == 1
    # Usage semantics stay in the real upload path.
    assert invite_row(fresh_db["db"], "tok-ok")["used_count"] == 1


def test_upload_public_enabled_tokenless_ok(client, fresh_db, spies):
    import app.app as appmod
    appmod.SETTINGS.public_upload_page_enabled = True
    try:
        r = _upload(client, token=None)
        assert r.status_code == 200
        assert spies["post"] == 1
    finally:
        appmod.SETTINGS.public_upload_page_enabled = False


# --------- token source ambiguity (AC #6) ---------

def test_upload_rejects_token_conflict(client, fresh_db, spies):
    seed_invite(fresh_db["db"], "tok-a", max_uses=-1)
    # Same token in form body and query string but different values -> conflict.
    r = client.post(
        "/api/upload?invite_token=tok-b",
        data={"item_id": "i", "session_id": "s", "invite_token": "tok-a"},
        files={"file": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")},
    )
    assert r.status_code == 400
    assert r.json().get("error") == "invite_token_conflict"
    assert spies["sha1"] == 0
    assert spies["post"] == 0


# --------- /api/upload/chunk/init ---------

def test_chunk_init_rejects_missing_token(client, fresh_db, spies):
    r = client.post("/api/upload/chunk/init", json={
        "item_id": "item-1", "session_id": "sess-1",
        "name": "f.bin", "size": 10,
    })
    assert r.status_code == 403
    # No chunk directory / manifest should have been created.
    assert not os.path.exists(_chunk_dir(fresh_db["chunk_root"]))


def test_chunk_init_valid_token_ok(client, fresh_db, spies):
    seed_invite(fresh_db["db"], "tok-ok", max_uses=-1)
    r = client.post("/api/upload/chunk/init", json={
        "item_id": "item-1", "session_id": "sess-1",
        "name": "f.bin", "size": 10, "invite_token": "tok-ok",
    })
    assert r.status_code == 200
    assert os.path.exists(os.path.join(_chunk_dir(fresh_db["chunk_root"]), "meta.json"))


def test_chunk_init_public_enabled_tokenless_ok(client, fresh_db, spies):
    import app.app as appmod
    appmod.SETTINGS.public_upload_page_enabled = True
    try:
        r = client.post("/api/upload/chunk/init", json={
            "item_id": "item-1", "session_id": "sess-1", "name": "f.bin", "size": 10,
        })
        assert r.status_code == 200
    finally:
        appmod.SETTINGS.public_upload_page_enabled = False


# --------- /api/upload/chunk (part write) ---------

def test_chunk_part_rejects_missing_token_and_writes_nothing(client, fresh_db, spies):
    r = client.post("/api/upload/chunk", data={
        "item_id": "item-1", "session_id": "sess-1",
        "chunk_index": "0", "total_chunks": "1",
    }, files={"chunk": ("f.part0", io.BytesIO(b"chunkdata"), "application/octet-stream")})
    assert r.status_code == 403
    d = _chunk_dir(fresh_db["chunk_root"])
    # Nothing written to disk.
    assert not os.path.exists(os.path.join(d, "part_000000"))


def test_chunk_part_valid_token_writes(client, fresh_db, spies):
    seed_invite(fresh_db["db"], "tok-ok", max_uses=-1)
    r = client.post("/api/upload/chunk", data={
        "item_id": "item-1", "session_id": "sess-1",
        "chunk_index": "0", "total_chunks": "1", "invite_token": "tok-ok",
    }, files={"chunk": ("f.part0", io.BytesIO(b"chunkdata"), "application/octet-stream")})
    assert r.status_code == 200
    d = _chunk_dir(fresh_db["chunk_root"])
    assert os.path.exists(os.path.join(d, "part_000000"))


# --------- /api/upload/chunk/complete ---------

def _prime_chunk_dir(chunk_root, total_chunks=1, payload=b"assembled"):
    d = _chunk_dir(chunk_root)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "f.bin", "total_chunks": total_chunks,
                   "content_type": "application/octet-stream"}, f)
    for i in range(total_chunks):
        with open(os.path.join(d, f"part_{i:06d}"), "wb") as f:
            f.write(payload)
    return d


def test_chunk_complete_rejects_before_assemble(client, fresh_db, spies):
    d = _prime_chunk_dir(fresh_db["chunk_root"])
    r = client.post("/api/upload/chunk/complete", json={
        "item_id": "item-1", "session_id": "sess-1", "name": "f.bin", "total_chunks": 1,
    })
    assert r.status_code == 403
    # Parts must NOT be read/assembled/cleaned up, and no Immich upload attempted.
    assert os.path.exists(os.path.join(d, "part_000000"))
    assert spies["sha1"] == 0
    assert spies["post"] == 0


def test_chunk_complete_valid_token_proceeds(client, fresh_db, spies):
    seed_invite(fresh_db["db"], "tok-ok", max_uses=-1)
    _prime_chunk_dir(fresh_db["chunk_root"])
    r = client.post("/api/upload/chunk/complete", json={
        "item_id": "item-1", "session_id": "sess-1", "name": "f.bin",
        "total_chunks": 1, "invite_token": "tok-ok",
    })
    assert r.status_code == 200
    assert spies["post"] == 1
    assert invite_row(fresh_db["db"], "tok-ok")["used_count"] == 1
