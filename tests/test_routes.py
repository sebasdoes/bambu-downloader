"""API tests for app/routes.py via FastAPI TestClient (lifespan on).

Covers: API-key gate, status, model listing/filtering, my-collections
(read + refresh), collection CRUD + sync trigger, share-target validation.
Network-dependent endpoints use monkeypatched manager/client internals.
"""

from __future__ import annotations

import importlib

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture()
def app_wired(tmp_env, monkeypatch):
    """Full app with fresh DB + routes wired; returns (testclient, db).

    Reload order matters: dependencies first, app.main LAST — main captures
    module references at import time (e.g. `from . import routes`), so it
    must re-import AFTER the others have been reloaded. The lifespan then
    wires THIS routes module with the fixture's singletons.
    """
    import app.config as config_mod
    import app.db as db_mod
    import app.downloader as dl_mod
    import app.makerworld as mw_mod
    import app.routes as routes_mod
    import app.scheduler as sched_mod

    for mod in (config_mod, db_mod, dl_mod, mw_mod, routes_mod, sched_mod):
        importlib.reload(mod)
    import app.main as main_mod

    importlib.reload(main_mod)

    import os

    from app.db import Database
    from app.downloader import DownloadManager
    from app.scheduler import SyncScheduler

    db_path = os.path.join(str(tmp_env), "test.db")
    database = Database(db_path)
    manager = DownloadManager(database)
    scheduler = SyncScheduler(database, manager)
    routes_mod.init(database, manager, scheduler)

    with fastapi_testclient.TestClient(main_mod.app) as client:
        yield client, database, manager


# ------------------------------------------------------------------ api key
def test_api_key_gate_disabled_by_default(app_wired):
    client, db, _ = app_wired
    resp = client.get("/api/status")
    assert resp.status_code == 200


def test_api_key_gate_enforced(app_wired, monkeypatch):
    client, db, _ = app_wired
    import app.config

    monkeypatch.setattr(app.config.settings, "api_key", "sekrit")
    resp = client.get("/api/status")
    assert resp.status_code == 401
    resp = client.get("/api/status", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401
    resp = client.get("/api/status", headers={"X-API-Key": "sekrit"})
    assert resp.status_code == 200


# ------------------------------------------------------------------- status
def test_status_shape(app_wired):
    client, db, _ = app_wired
    resp = client.get("/api/status")
    body = resp.json()
    assert resp.status_code == 200
    for key in (
        "authenticated",
        "email",
        "token_invalid",
        "region",
        "download_dir",
        "model_count",
        "collection_count",
        "scheduler",
    ):
        assert key in body
    assert body["authenticated"] is False
    assert body["model_count"] == 0


# ------------------------------------------------------------------- models
def test_models_listing_and_filters(app_wired):
    client, db, _ = app_wired
    for did, cid in [(1, None), (2, 10), (3, 10)]:
        db.insert_model(
            design_id=did,
            profile_id=None,
            title=f"m{did}",
            slug="s",
            url="u",
            filename=f"{did}.3mf",
            file_path=f"/x/{did}.3mf",
            file_size=1,
            collection_id=cid,
            collection_title=f"C{cid}" if cid else None,
        )
    resp = client.get("/api/models")
    body = resp.json()
    assert body["total"] == 3
    resp = client.get("/api/models", params={"collection_id": 10})
    assert resp.json()["total"] == 2
    resp = client.get("/api/models", params={"no_collection": "true"})
    assert resp.json()["total"] == 1
    resp = client.get("/api/models", params={"label": "C10"})
    assert resp.json()["total"] == 2
    # pagination
    resp = client.get("/api/models", params={"limit": 1, "offset": 0})
    assert len(resp.json()["models"]) == 1
    resp = client.get("/api/models", params={"limit": 1, "offset": 2})
    assert len(resp.json()["models"]) == 1
    assert resp.json()["total"] == 3
    # labels endpoint
    resp = client.get("/api/model-labels")
    labels = {lbl["label"] for lbl in resp.json()}
    assert "C10" in labels
    assert "Manual download" in labels


# ----------------------------------------------------------- my-collections
def test_my_collections_empty_before_refresh(app_wired):
    client, db, _ = app_wired
    resp = client.get("/api/my-collections")
    body = resp.json()
    assert resp.status_code == 200
    assert body["collections"] == []
    assert body["fetched_at"] is None
    assert body["authenticated"] is False


def test_my_collections_requires_token_for_refresh(app_wired):
    client, db, _ = app_wired
    resp = client.post("/api/my-collections/refresh")
    assert resp.status_code == 401


def test_my_collections_refresh_and_read(app_wired, monkeypatch):
    client, db, manager = app_wired
    db.set_meta("bambu_token", "tok")
    db.insert_model(
        design_id=11,
        profile_id=None,
        title="x",
        slug="x",
        url="u",
        filename="x.3mf",
        file_path="/x/x.3mf",
        file_size=1,
    )

    async def fake_refresh(self):
        db.replace_remote_collections(
            [
                {
                    "collection_id": 1,
                    "title": "A",
                    "slug": "a",
                    "design_count": 2,
                    "is_default": False,
                    "design_ids": [11, 12],
                },
            ]
        )
        return {"collections": 1, "fetched_at": db.remote_collections_fetched_at()}

    monkeypatch.setattr(type(manager), "refresh_my_collections", fake_refresh)
    resp = client.post("/api/my-collections/refresh")
    assert resp.status_code == 200
    assert resp.json()["collections"] == 1

    resp = client.get("/api/my-collections")
    body = resp.json()
    assert body["authenticated"] is True
    assert body["fetched_at"] is not None
    assert len(body["collections"]) == 1, body["collections"]
    coll = body["collections"][0]
    assert coll["checked_ids"] == [11]
    assert coll["downloaded_count"] == 1
    assert coll["downloaded"] is False
    assert coll["followed"] is False
    assert coll["sync_interval_minutes"] is None

    # after following, the flag flips
    db.upsert_collection(1, "A", "https://mw/collections/1", 60)
    resp = client.get("/api/my-collections")
    assert resp.json()["collections"][0]["followed"] is True
    assert resp.json()["collections"][0]["sync_interval_minutes"] == 60


# ---------------------------------------------------------------- download
# ---------------------------------------------------------------- download
def test_download_endpoint_maps_errors(app_wired, monkeypatch):
    from app.makerworld import (
        AuthRequiredError,
        CaptchaError,
        ForbiddenError,
        MakerWorldError,
        NotFoundError,
    )

    client, db, manager = app_wired

    def make(kind):
        async def _dl(self, url, collection_id=None, subfolder=None):
            raise kind

        return _dl

    cases = [
        (AuthRequiredError("auth"), 401),
        (NotFoundError("nf"), 404),
        (ForbiddenError("forb"), 403),
        (CaptchaError(), 429),
        (MakerWorldError("other"), 400),
    ]
    for err, code in cases:
        monkeypatch.setattr(type(manager), "download_model", make(err))
        resp = client.post(
            "/api/download", json={"url": "https://makerworld.com/en/models/1"}
        )
        assert resp.status_code == code, (err, resp.status_code)


def test_download_success_and_exists(app_wired, monkeypatch):
    client, db, manager = app_wired

    async def fake_download(self, url, collection_id=None, subfolder=None):
        if "1" in url:
            return {"status": "downloaded", "title": "T", "path": "/x", "size": 10}
        return {"status": "exists"}

    monkeypatch.setattr(type(manager), "download_model", fake_download)
    resp = client.post(
        "/api/download", json={"url": "https://makerworld.com/en/models/1"}
    )
    assert resp.json()["status"] == "downloaded"
    resp = client.post(
        "/api/download", json={"url": "https://makerworld.com/en/models/2"}
    )
    assert resp.json()["status"] == "exists"


def test_download_requires_url(app_wired):
    client, db, _ = app_wired
    resp = client.post("/api/download", json={})
    assert resp.status_code == 422  # pydantic validation


# -------------------------------------------------------------- collections
def test_add_collection_validates_url(app_wired):
    client, db, _ = app_wired
    resp = client.post(
        "/api/collections", json={"url": "https://makerworld.com/en/models/1"}
    )
    assert resp.status_code == 400


def test_add_collection_private_hint(app_wired, monkeypatch):
    client, db, manager = app_wired
    from app.makerworld import ForbiddenError

    class FakeClient:
        auth_token = "tok"

        async def get_collection_info(self, cid):
            raise ForbiddenError("no access")

        async def close(self):
            return None

    # get_client (pooled) is what add_collection actually uses now
    monkeypatch.setattr("app.routes.get_client", lambda **kw: FakeClient())
    db.set_meta("bambu_token", "tok")
    resp = client.post(
        "/api/collections", json={"url": "https://makerworld.com/en/collections/123-c"}
    )
    assert resp.status_code == 403
    assert "private" in resp.json()["detail"].lower()


def test_collection_patch_and_delete(app_wired):
    client, db, _ = app_wired
    db.upsert_collection(1, "C", "u", 60)
    resp = client.patch(
        "/api/collections/1", json={"enabled": False, "sync_interval_minutes": 30}
    )
    body = resp.json()
    assert body["enabled"] == 0
    assert body["sync_interval_minutes"] == 30
    # delete without files
    resp = client.delete("/api/collections/1")
    assert resp.status_code == 200
    assert db.get_collection(1) is None


def test_collection_delete_404(app_wired):
    client, db, _ = app_wired
    resp = client.delete("/api/collections/999")
    assert resp.status_code == 404


def test_sync_now_requires_token(app_wired):
    client, db, _ = app_wired
    db.upsert_collection(1, "C", "u", 60)
    resp = client.post("/api/collections/1/sync")
    assert resp.status_code == 401


def test_sync_now_starts_background_task(app_wired, monkeypatch):
    client, db, manager = app_wired
    db.upsert_collection(1, "C", "u", 60)
    db.set_meta("bambu_token", "tok")
    started = []

    def fake_trigger(mgr, cid):
        started.append(cid)
        return True

    monkeypatch.setattr("app.routes.trigger_sync", fake_trigger)
    resp = client.post("/api/collections/1/sync")
    assert resp.json() == {"started": True}
    assert started == [1]
    # double-trigger reports already-running (fake returns False next time)
    monkeypatch.setattr("app.routes.trigger_sync", lambda mgr, cid: False)
    resp = client.post("/api/collections/1/sync")
    assert resp.json() == {"started": False}


# ------------------------------------------------------------- shared model
def test_shared_model_validation(app_wired):
    client, db, _ = app_wired
    ok = client.get(
        "/api/shared-model", params={"url": "https://makerworld.com/en/models/5-x"}
    )
    assert ok.json() == {"valid": True, "url": "https://makerworld.com/en/models/5-x"}
    bad = client.get("/api/shared-model", params={"url": "https://example.com/nope"})
    assert bad.json()["valid"] is False


# ---------------------------------------------------------------- events
def test_events_endpoint(app_wired):
    client, db, manager = app_wired
    import asyncio

    from app.downloader import add_event

    # TestClient runs an event loop per request; add via fresh loop
    anyio_loop = asyncio.new_event_loop()
    anyio_loop.run_until_complete(add_event("sync", "hello test"))
    anyio_loop.close()
    resp = client.get("/api/events")
    messages = [e["message"] for e in resp.json()["events"]]
    assert "hello test" in messages


# ---------------------------------------------------------------- logout
def test_logout_clears_state(app_wired, monkeypatch):
    client, db, _ = app_wired
    db.set_meta("bambu_token", "tok")
    db.set_meta("bambu_email", "a@b.c")
    resp = client.post("/api/auth/logout")
    assert resp.json()["ok"] is True
    assert db.get_meta("bambu_token") is None
    assert db.get_meta("bambu_email") is None
