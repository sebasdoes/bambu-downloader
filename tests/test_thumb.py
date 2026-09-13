"""API tests for /thumb ETag + 304 revalidation and static shell endpoints."""

from __future__ import annotations

import importlib

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

# A minimal 1x1 PNG so _sniff_media returns image/png deterministically.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082"
)

COVER_URL = "https://makerworld.bblmw.com/makerworld/model/TEST/design/cover.jpg"


@pytest.fixture()
def thumb_env(tmp_env, monkeypatch):
    """App wired with one model whose cover.webp exists on disk."""
    import app.db as db_mod
    import app.downloader as dl_mod
    import app.main as main_mod
    import app.makerworld as mw_mod
    import app.routes as routes_mod
    import app.scheduler as sched_mod

    for mod in (main_mod, db_mod, dl_mod, mw_mod, routes_mod, sched_mod):
        importlib.reload(mod)

    import os

    from app.db import Database
    from app.downloader import DownloadManager
    from app.scheduler import SyncScheduler

    database = Database(os.path.join(str(tmp_env), "test.db"))
    dl_root = __import__("pathlib").Path(str(tmp_env)) / "downloads" / "cover-test"
    dl_root.mkdir(parents=True, exist_ok=True)
    (dl_root / "cover.webp").write_bytes(PNG_1PX)
    database.insert_model(
        design_id=999,
        profile_id=None,
        title="T",
        slug="t",
        url="u",
        filename="t.3mf",
        file_path=str(dl_root / "t.3mf"),
        file_size=1,
        cover_url=COVER_URL,
    )
    routes_mod.init(
        database,
        DownloadManager(database),
        SyncScheduler(database, DownloadManager(database)),
    )

    from app.main import app

    with fastapi_testclient.TestClient(app) as client:
        yield client, database, dl_root


def test_thumb_serves_local_copy_with_etag(thumb_env):
    client, db, dl_root = thumb_env
    resp = client.get("/thumb", params={"url": COVER_URL})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")
    assert "etag" in resp.headers
    assert "max-age=86400" in resp.headers["cache-control"]
    assert len(resp.content) == len(PNG_1PX)


def test_thumb_304_on_matching_etag(thumb_env):
    client, db, _ = thumb_env
    first = client.get("/thumb", params={"url": COVER_URL})
    etag = first.headers["etag"]
    reval = client.get(
        "/thumb", params={"url": COVER_URL}, headers={"If-None-Match": etag}
    )
    assert reval.status_code == 304
    assert reval.content == b""
    assert reval.headers["etag"] == etag


def test_thumb_200_on_mismatched_etag(thumb_env):
    client, db, _ = thumb_env
    resp = client.get(
        "/thumb", params={"url": COVER_URL}, headers={"If-None-Match": '"stale"'}
    )
    assert resp.status_code == 200
    assert resp.content == PNG_1PX


def test_thumb_304_list_header(thumb_env):
    """If-None-Match may carry multiple candidates (comma-separated)."""
    client, db, _ = thumb_env
    etag = client.get("/thumb", params={"url": COVER_URL}).headers["etag"]
    resp = client.get(
        "/thumb",
        params={"url": COVER_URL},
        headers={"If-None-Match": f'"other", {etag}'},
    )
    assert resp.status_code == 304


def test_thumb_etag_changes_when_file_changes(thumb_env):
    client, db, dl_root = thumb_env
    etag1 = client.get("/thumb", params={"url": COVER_URL}).headers["etag"]
    (dl_root / "cover.webp").write_bytes(PNG_1PX + b"x")  # mutate the file
    etag2 = client.get("/thumb", params={"url": COVER_URL}).headers["etag"]
    assert etag1 != etag2


def test_thumb_rejects_bad_inputs(thumb_env):
    client, db, _ = thumb_env
    resp = client.get("/thumb", params={"url": "http://insecure/x.jpg"})
    assert resp.status_code == 400
    resp = client.get("/thumb", params={"url": "https://evil.example.com/x.jpg"})
    assert resp.status_code == 400


def test_thumb_clamps_width(thumb_env, monkeypatch):
    """w is clamped 64..1920 — verified via the CDN path's monkeypatch."""
    client, db, dl_root = thumb_env
    seen = {}

    async def fake_fetch(self, url, width=512):
        seen["w"] = width
        return PNG_1PX

    import app.makerworld as mw_mod

    monkeypatch.setattr(mw_mod.MakerWorldClient, "fetch_thumbnail", fake_fetch)
    # CDN path (no local cover): the route PERSISTS cover.webp after each
    # fetch, so delete it between requests to force the CDN path again.
    dl_root.joinpath("cover.webp").unlink(missing_ok=True)
    resp = client.get("/thumb", params={"url": COVER_URL, "w": 99999})
    assert resp.status_code == 200
    assert seen["w"] == 1920  # upper clamp
    dl_root.joinpath("cover.webp").unlink(missing_ok=True)
    resp = client.get("/thumb", params={"url": COVER_URL, "w": 1})
    assert seen["w"] == 64  # lower clamp


# ------------------------------------------------------------- static shell
def test_shell_cache_headers(thumb_env):
    client, _, _ = thumb_env
    for path, must_cache in [
        ("/", False),
        ("/sw.js", False),
        ("/manifest.webmanifest", None),
    ]:
        resp = client.get(path)
        assert resp.status_code == 200
        cc = resp.headers.get("cache-control", "")
        if not must_cache:
            assert "no-store" in cc or path == "/manifest.webmanifest"


def test_sw_cache_version_bumped(thumb_env):
    """The SW shell list must reference the current version constant."""
    client, _, _ = thumb_env
    sw = client.get("/sw.js").text
    assert "CACHE_VERSION = 'v9'" in sw
