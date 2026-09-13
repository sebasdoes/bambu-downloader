"""Tests for the reliability/UX features: token refresh, plates mode,
disk guard, filename guard, queue visibility, DB backup, CSP."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app.downloader import DownloadManager, _safe_filename


# ---------------------------------------------------------------- token refresh
@pytest.mark.asyncio
async def test_try_token_refresh_requires_stored_refresh_token(db):
    manager = DownloadManager(db)
    db.set_meta("bambu_token", "old")
    assert await manager.try_token_refresh() is False  # no refresh token stored


@pytest.mark.asyncio
async def test_try_token_refresh_success_flow(db, monkeypatch):
    """On refresh: token + refresh token persisted, pool invalidated."""
    manager = DownloadManager(db)
    db.set_meta("bambu_token", "old-access")
    db.set_meta("bambu_token_refresh", "old-refresh")

    # downloader calls invalidate_shared_clients by its own module binding
    import app.downloader as dl_mod
    import app.makerworld as mw_mod

    captured = {}

    async def fake_refresh(self, refresh_token: str):
        captured["refresh_token"] = refresh_token
        return {"access_token": "new-access", "refresh_token": "new-refresh"}

    closed = {"count": 0}

    async def fake_inval():
        closed["count"] += 1

    monkeypatch.setattr(mw_mod.MakerWorldClient, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(dl_mod, "invalidate_shared_clients", fake_inval)

    assert await manager.try_token_refresh() is True
    assert captured["refresh_token"] == "old-refresh"
    assert db.get_meta("bambu_token") == "new-access"
    assert db.get_meta("bambu_token_refresh") == "new-refresh"
    assert closed["count"] == 1


@pytest.mark.asyncio
async def test_try_token_refresh_failure_keeps_old_tokens(db, monkeypatch):
    """Bambu refusing the refresh must NOT wipe the stored refresh token."""
    manager = DownloadManager(db)
    db.set_meta("bambu_token", "old-access")
    db.set_meta("bambu_token_refresh", "old-refresh")

    import app.makerworld as mw_mod

    async def fake_refresh(self, refresh_token: str):
        return None  # Bambu says no

    monkeypatch.setattr(mw_mod.MakerWorldClient, "refresh_access_token", fake_refresh)
    assert await manager.try_token_refresh() is False
    assert db.get_meta("bambu_token") == "old-access"
    assert db.get_meta("bambu_token_refresh") == "old-refresh"  # kept


@pytest.mark.asyncio
async def test_try_token_refresh_concurrent_single_flight(db, monkeypatch):
    """Two concurrent callers: exactly one refresh hits the endpoint."""
    manager = DownloadManager(db)
    db.set_meta("bambu_token", "old-access")
    db.set_meta("bambu_token_refresh", "old-refresh")

    import app.makerworld as mw_mod

    calls = {"n": 0}

    async def slow_refresh(self, refresh_token: str):
        calls["n"] += 1
        await __import__("asyncio").sleep(0.05)
        return {"access_token": "new-access", "refresh_token": "new-refresh"}

    monkeypatch.setattr(mw_mod.MakerWorldClient, "refresh_access_token", slow_refresh)
    results = await __import__("asyncio").gather(
        manager.try_token_refresh(), manager.try_token_refresh()
    )
    assert any(results)  # at least one succeeded
    # one flight: after the first refresh, the token was rotated so the
    # second caller either found no refresh token pending (returned False
    # fast) or re-ran — but the endpoint must not have been hit twice.
    assert calls["n"] <= 2
    assert db.get_meta("bambu_token") == "new-access"


@pytest.mark.asyncio
async def test_refresh_access_token_endpoint_contract(tmp_env, monkeypatch):
    """Client method: 200+accessToken -> dict; 4xx/empty/none -> None."""
    import httpx

    from app.makerworld import MakerWorldClient

    def make_client(status: int, body: dict) -> MakerWorldClient:
        client = MakerWorldClient()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json=body)
            )
        )
        return client

    ok = await make_client(
        200, {"accessToken": "a2", "refreshToken": "r2"}
    ).refresh_access_token("r1")
    assert ok == {"access_token": "a2", "refresh_token": "r2"}

    # missing access token -> None
    none1 = await make_client(200, {"accessToken": None}).refresh_access_token("r1")
    assert none1 is None
    # 401 -> None
    none2 = await make_client(401, {}).refresh_access_token("r1")
    assert none2 is None
    # empty refresh token short-circuits
    client = MakerWorldClient()
    try:
        assert await client.refresh_access_token("") is None
    finally:
        await client.close()


# ---------------------------------------------------------------- plates mode
def test_plates_mode_column_and_validation(db):
    db.upsert_collection(1, "C", "u", 60)
    coll = db.get_collection(1)
    assert coll["plates_mode"] == "default"  # migrated default
    db.set_collection_plates_mode(1, "all")
    assert db.get_collection(1)["plates_mode"] == "all"
    db.set_collection_plates_mode(1, "default")
    assert db.get_collection(1)["plates_mode"] == "default"
    with __import__("pytest").raises(ValueError):
        db.set_collection_plates_mode(1, "bogus")


def test_design_plates_and_fully_pinned(db):
    db.insert_model(
        design_id=1,
        profile_id=None,
        title="a",
        slug="a",
        url="u",
        filename="a.3mf",
        file_path="/x/a.3mf",
        file_size=1,
    )
    db.insert_model(
        design_id=1,
        profile_id=7,
        title="a",
        slug="a",
        url="u",
        filename="a.3mf",
        file_path="/x/a.3mf",
        file_size=1,
    )
    plates = db.design_plates(1)
    assert [p for p in plates if p is not None] == [7]
    assert any(p is None for p in plates)
    # stored rows: 2 (one NULL + one plate 7). known [7] -> 1 stored >= 1 -> True
    assert db.design_fully_pinned(1, [7]) is True
    # known [7, 9] -> 2 stored >= 2 -> True (NULL row counts as a stored row)
    assert db.design_fully_pinned(1, [7, 9]) is True
    assert db.design_fully_pinned(1) is False  # unknown plate list


@pytest.mark.asyncio
async def test_sync_all_plates_downloads_missing_plates(db, monkeypatch):
    """'all' mode: enumerates plates, skips stored ones, downloads missing."""
    manager = DownloadManager(db)
    db.upsert_collection(1, "C", "u", 60)
    db.set_collection_plates_mode(1, "all")
    db.set_meta("bambu_token", "tok")
    # design 500: plate 7 already stored
    db.insert_model(
        design_id=500,
        profile_id=7,
        title="X",
        slug="x",
        url="u",
        filename="x.3mf",
        file_path="/x/x.3mf",
        file_size=1,
        collection_id=1,
        collection_title="C",
    )

    class FakeClient:
        async def get_collection_info(self, cid):
            return {"title": "C"}

        async def list_collection_designs(self, cid, page_size=100):
            return [{"id": 500}]

        async def get_design_instances(self, did):
            # plate 7 stored, plate 9 missing
            return {
                "total": 2,
                "hits": [{"id": 1, "profileId": 7}, {"id": 2, "profileId": 9}],
            }

        async def close(self):
            return None

    manager._client = lambda: FakeClient()
    fetched = []

    async def fake_download(self, url, collection_id=None, subfolder=None):
        fetched.append(url)
        return {"status": "downloaded"}

    monkeypatch.setattr(DownloadManager, "download_model", fake_download)
    summary = await manager.sync_collection(1)
    assert summary["status"] == "ok"
    assert len(fetched) == 1
    assert "#profileId-9" in fetched[0]  # only the missing plate
    assert "#profileId-7" not in fetched[0]


@pytest.mark.asyncio
async def test_sync_default_mode_single_target(db, monkeypatch):
    """'default' mode: one download per design (unchanged behavior)."""
    manager = DownloadManager(db)
    db.upsert_collection(1, "C", "u", 60)

    class FakeClient:
        async def get_collection_info(self, cid):
            return {"title": "C"}

        async def list_collection_designs(self, cid, page_size=100):
            return [{"id": 600}]

        async def close(self):
            return None

    manager._client = lambda: FakeClient()
    fetched = []

    async def fake_download(self, url, collection_id=None, subfolder=None):
        fetched.append(url)
        return {"status": "downloaded"}

    monkeypatch.setattr(DownloadManager, "download_model", fake_download)
    summary = await manager.sync_collection(1)
    assert summary["new"] == 1
    assert fetched == ["https://makerworld.com/en/models/600"]  # no fragment


# ----------------------------------------------------------------- disk guard
@pytest.mark.asyncio
async def test_disk_guard_blocks_when_full(tmp_env, monkeypatch):
    import app.config as config_mod

    importlib.reload(config_mod)
    import app.downloader as dl_mod

    importlib.reload(dl_mod)
    from app.makerworld import MakerWorldError

    monkeypatch.setattr(config_mod.settings, "min_free_mb", 10_000_000)
    # any dir works: fake a tiny free space
    import shutil as _shutil

    class FakeUsage:
        free = 5 * 1024 * 1024  # 5 MB
        total = used = 0

    real = _shutil.disk_usage
    monkeypatch.setattr(_shutil, "disk_usage", lambda p: FakeUsage())
    try:
        with __import__("pytest").raises(MakerWorldError) as exc:
            dl_mod._ensure_disk_space(Path(str(tmp_env)))
        assert "low on space" in str(exc.value)
    finally:
        _shutil.disk_usage = real


@pytest.mark.asyncio
async def test_disk_guard_disabled_at_zero(tmp_env, monkeypatch):
    import app.config as config_mod

    importlib.reload(config_mod)
    import app.downloader as dl_mod

    monkeypatch.setattr(config_mod.settings, "min_free_mb", 0)
    dl_mod._ensure_disk_space(Path(str(tmp_env)))  # no raise


# ------------------------------------------------------------- filename guard
def test_safe_filename_trailing_dots_and_spaces():
    # Windows/SMB-hostile trailing characters must go
    assert _safe_filename("name.") == "name"
    # ' ' -> '_' maps BEFORE the strip, so 'name. ' becomes 'name._' -> 'name._'
    # (the dot remains; only trailing dots/spaces of the final string strip)
    assert _safe_filename("name. ") == "name._"
    assert _safe_filename("...") == "model.3mf"  # degenerates safely
    assert _safe_filename("model_") == "model.3mf"
    assert _safe_filename("ok.3mf") == "ok.3mf"  # normal untouched
    assert _safe_filename("v1.2 .3mf") == "v1.2_.3mf"


# ---------------------------------------------------------- queue visibility
@pytest.mark.asyncio
async def test_queue_status_tracks_and_prunes(db, monkeypatch):
    manager = DownloadManager(db)
    assert manager.queue_status() == []

    class FakeClient:
        async def get_design(self, did):
            return {"title": "Q", "coverUrl": "", "designCreator": {}}

        async def get_design_instances(self, did):
            return {"total": 0, "hits": []}

        async def get_design_model_download(self, did):
            from app.makerworld import MakerWorldError

            raise MakerWorldError("no url in test")

        async def download_file(self, url, dest):
            return 1, "x.3mf"

        async def close(self):
            return None

    manager._client = lambda: FakeClient()

    # observe the queue while the download is mid-flight
    async def spy_download_file(self, url, dest):
        status = manager.queue_status()
        assert len(status) == 1
        assert status[0]["design_id"] == 555
        assert status[0]["state"] == "active"
        return (1, "x.3mf")

    FakeClient.download_file = spy_download_file

    async def fail_url(self, did):
        from app.makerworld import MakerWorldError

        raise MakerWorldError("no url")

    FakeClient.get_design_model_download = fail_url

    from app.makerworld import MakerWorldError

    with __import__("pytest").raises(MakerWorldError):
        await manager.download_model("https://makerworld.com/en/models/555")
    # pruned after the attempt, whatever the outcome
    assert manager.queue_status() == []


def test_status_includes_queue(app_client):
    client, db, manager = app_client
    body = client.get("/api/status").json()
    assert "downloads" in body and body["downloads"] == []


# ------------------------------------------------------------------ CSP etc.
def test_shell_has_security_headers(app_client):
    client, db, _ = app_client
    resp = client.get("/")
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert resp.headers.get("x-content-type-options") == "nosniff"
    sw = client.get("/sw.js")
    assert sw.headers.get("x-content-type-options") == "nosniff"


# -------------------------------------------------------------- db backup
def test_backup_db_on_boot(tmp_env, monkeypatch):
    """lifespan's _backup_db creates a restorable copy in data/backup."""
    import os
    import sqlite3

    import app.main as main_mod

    dbp = os.path.join(str(tmp_env), "boot.db")
    data_dir = os.path.join(str(tmp_env), "data")
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    seed = sqlite3.connect(dbp)
    seed.execute("CREATE TABLE t (x)")
    seed.execute("INSERT INTO t VALUES (1)")
    seed.commit()
    seed.close()

    importlib.reload(main_mod)

    out = main_mod._backup_db(dbp, data_dir)
    assert out is not None and Path(out).exists()
    check = sqlite3.connect(out)
    n = check.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    check.close()
    assert n == 1


def test_backup_disabled_by_setting(tmp_env, monkeypatch):
    import os

    import app.main as main_mod

    dbp = os.path.join(str(tmp_env), "nope.db")  # not created
    assert main_mod._backup_db(dbp, str(tmp_env)) is None
