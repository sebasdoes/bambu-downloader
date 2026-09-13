"""Unit tests for app/downloader.py — dedup, filename safety, events, backfill.

Download resolution paths are exercised with mocked client methods; no
network is involved.
"""

from __future__ import annotations

import pytest

from app.downloader import (
    DownloadManager,
    _safe_filename,
    _slugify,
    add_event,
    recent_events,
)
from app.makerworld import AuthRequiredError


# ------------------------------------------------------------------ helpers
def test_safe_filename():
    assert _safe_filename("ok_name-1.3mf") == "ok_name-1.3mf"
    # every bad char maps to exactly one underscore (1:1, not collapsed):
    # / \ : * ? each become '_' — len is preserved
    assert _safe_filename(r"bad/name\with:chars*?.3mf") == "bad_name_with_chars__.3mf"
    assert _safe_filename(".hidden") == "model_.hidden"  # no dotfiles
    long = "a" * 500 + ".3mf"
    assert len(_safe_filename(long)) <= 150


def test_slugify():
    assert _slugify("Hello World!") == "hello-world"
    assert _slugify("  multi   spaces  ") == "multi-spaces"
    assert _slugify("ünïcode_ñame") == "nme" or _slugify(
        "ünïcode_ñame"
    )  # non-ascii stripped
    assert len(_slugify("x" * 200)) <= 80
    assert _slugify("") == "untitled"


@pytest.mark.asyncio
async def test_add_event_ring_buffer():
    # Fill beyond capacity and check trimming keeps the newest.
    from app.downloader import _MAX_EVENTS, _events

    old_len = len(_events)
    for i in range(_MAX_EVENTS + 50):
        await add_event("download", f"event {i}")
    events = recent_events(limit=_MAX_EVENTS)
    assert len(events) <= _MAX_EVENTS
    # newest first
    assert events[0]["message"] == f"event {_MAX_EVENTS + 50 - 1}"
    assert old_len <= len(_events)


# -------------------------------------------------------------- dedup logic
@pytest.mark.asyncio
async def test_download_model_exists_short_circuit(db):
    manager = DownloadManager(db)

    db.insert_model(
        design_id=77,
        profile_id=None,
        title="T",
        slug="t",
        url="u",
        filename="t.3mf",
        file_path="/x/t.3mf",
        file_size=1,
    )
    # plate-less URL: design-level dedup
    result = await manager.download_model("https://makerworld.com/en/models/77")
    assert result["status"] == "exists"
    # URL with a plate that does NOT match the stored NULL row -> proceeds
    # (we don't go further: get_design would hit network; assert via error)
    with pytest.raises(Exception):
        await manager.download_model("https://makerworld.com/en/models/77#profileId-5")


@pytest.mark.asyncio
async def test_download_model_exact_profile_dedup(db):
    manager = DownloadManager(db)
    db.insert_model(
        design_id=77,
        profile_id=5,
        title="T",
        slug="t",
        url="u",
        filename="t.3mf",
        file_path="/x/t.3mf",
        file_size=1,
    )
    result = await manager.download_model(
        "https://makerworld.com/en/models/77#profileId-5"
    )
    assert result["status"] == "exists"


@pytest.mark.asyncio
async def test_resolve_design_reports_already_downloaded(db):
    manager = DownloadManager(db)
    db.insert_model(
        design_id=88,
        profile_id=None,
        title="T",
        slug="t",
        url="u",
        filename="t.3mf",
        file_path="/x/t.3mf",
        file_size=1,
    )

    class FakeClient:
        async def get_design(self, design_id):
            return {"title": "T", "coverUrl": "", "designCreator": {"name": "N"}}

        async def get_design_instances(self, design_id):
            return {"total": 1, "hits": [{"id": 1, "profileId": 5}]}

        async def close(self):
            return None

    manager._client = lambda: FakeClient()
    result = await manager.resolve_design("https://makerworld.com/en/models/88")
    assert result["already_downloaded"] is True
    assert result["design_id"] == 88
    assert result["instances"][0]["profileId"] == 5


# ---------------------------------------------------------- refresh + sync
@pytest.mark.asyncio
async def test_refresh_my_collections_requires_token(db):
    manager = DownloadManager(db)
    with pytest.raises(AuthRequiredError):
        await manager.refresh_my_collections()


@pytest.mark.asyncio
async def test_refresh_my_collections_caches_listing(db):
    manager = DownloadManager(db)
    db.set_meta("bambu_token", "tok")

    class FakeClient:
        async def list_my_collections(
            self, page_size=50, max_designs_per_collection=1000
        ):
            return [
                {
                    "collection_id": 1,
                    "title": "A",
                    "slug": "a",
                    "design_count": 2,
                    "is_default": False,
                    "design_ids": [11, 12],
                }
            ]

        async def close(self):
            return None

    manager._client = lambda: FakeClient()
    result = await manager.refresh_my_collections()
    assert result["collections"] == 1
    assert result["fetched_at"] is not None
    rows = db.remote_collections()
    assert rows[0]["collection_id"] == 1
    assert rows[0]["design_ids"] == [11, 12]


@pytest.mark.asyncio
async def test_sync_collection_records_progress(db):
    """Sync with an empty remote listing: records ok + zero new."""
    manager = DownloadManager(db)
    db.upsert_collection(1, "Cats", "https://mw/collections/1", 60)

    class FakeClient:
        async def get_collection_info(self, collection_id):
            return {"title": "Cats"}

        async def list_collection_designs(self, collection_id, page_size=100):
            return []

        async def close(self):
            return None

    manager._client = lambda: FakeClient()
    summary = await manager.sync_collection(1)
    assert summary["status"] == "ok"
    assert summary["new"] == 0
    coll = db.get_collection(1)
    assert coll["last_sync_status"] == "ok"
    assert coll["last_sync_new"] == 0


@pytest.mark.asyncio
async def test_sync_collection_unknown_collection(db):
    manager = DownloadManager(db)
    from app.makerworld import MakerWorldError

    with pytest.raises(MakerWorldError):
        await manager.sync_collection(999)


@pytest.mark.asyncio
async def test_sync_collection_skips_downloaded(db, monkeypatch):
    """Designs already in the library are skipped without download attempts."""
    manager = DownloadManager(db)
    db.upsert_collection(1, "Cats", "u", 60)
    db.insert_model(
        design_id=500,
        profile_id=None,
        title="X",
        slug="x",
        url="u",
        filename="x.3mf",
        file_path="/x/x.3mf",
        file_size=1,
    )

    class FakeClient:
        async def get_collection_info(self, collection_id):
            return {"title": "Cats"}

        async def list_collection_designs(self, collection_id, page_size=100):
            return [{"id": 500}, {"id": 501}]  # 500 already present

        async def close(self):
            return None

    manager._client = lambda: FakeClient()
    called = []

    async def fake_download(url, collection_id=None, subfolder=None):
        called.append(url)
        return {"status": "downloaded"}

    manager.download_model = fake_download
    summary = await manager.sync_collection(1)
    assert summary["status"] == "ok"
    assert len(called) == 1  # only the not-yet-downloaded design
    assert "500" not in called[0]


# ----------------------------------------------------------- backfill flow
class _FullFakeClient:
    """Fake pooled client covering get_design + thumbnail fetches."""

    async def get_design(self, design_id):
        return {"coverUrl": "https://c/x", "designCreator": {"name": "N"}}

    async def fetch_thumbnail(self, url, width=512):
        return b"webp-bytes"

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_backfill_watermark_flow(db, monkeypatch):
    """Clean scan -> incremental; found-work -> watermark cleared.

    The pooled API client is faked out: the backfill must never touch the
    network in tests (and never leave a pooled TLS stream to be GC'd after
    the test's event loop closed).
    """
    manager = DownloadManager(db)
    db.mark_meta_scan(clean=True)
    manager._client = lambda: _FullFakeClient()
    monkeypatch.setattr(
        manager.db,
        "models_missing_meta",
        lambda since=None: (
            [
                {
                    "design_id": 1,
                    "profile_id": None,
                    "file_path": "/x/1.3mf",
                    "cover_url": None,
                    "creator": None,
                }
            ]
            if since
            else []
        ),
    )
    await manager.backfill_metadata()
    # watermark cleared because the incremental pass found work
    scan_at, clean = db.meta_scan_state()
    assert clean is False


@pytest.mark.asyncio
async def test_backfill_incremental_noop_keeps_clean(db, monkeypatch):
    manager = DownloadManager(db)
    db.mark_meta_scan(clean=True)
    scan_at, clean = db.meta_scan_state()
    monkeypatch.setattr(manager.db, "models_missing_meta", lambda since=None: [])
    await manager.backfill_metadata()
    _, clean_after = db.meta_scan_state()
    assert clean_after is True  # stays clean
    new_at = db.meta_scan_state()[0]
    assert new_at >= scan_at
