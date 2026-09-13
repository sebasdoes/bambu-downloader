"""Unit tests for app/db.py — schema, models, collections, cache, watermark."""

from __future__ import annotations

import sqlite3

from app.db import Database, utcnow


# ---------------------------------------------------------------- schema
def test_schema_created(db: Database):
    conn = sqlite3.connect(db.db_path)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert {"meta", "models", "collections", "remote_collections"} <= tables


def test_hot_path_indexes_created(db: Database):
    """The /thumb + list paths must be indexed (medium-opt #5)."""
    conn = sqlite3.connect(db.db_path)
    idx = {r[1] for r in conn.execute("PRAGMA index_list(models)")}
    conn.close()
    assert {"idx_models_cover", "idx_models_created", "idx_models_collection"} <= idx


def test_wal_and_synchronous_normal(db: Database):
    """Every connection must set WAL + synchronous=NORMAL."""
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


# ---------------------------------------------------------------- meta
def test_meta_roundtrip(db: Database):
    assert db.get_meta("missing") is None
    db.set_meta("k", "v1")
    assert db.get_meta("k") == "v1"
    db.set_meta("k", "v2")  # upsert
    assert db.get_meta("k") == "v2"
    db.delete_meta("k")
    assert db.get_meta("k") is None


# ---------------------------------------------------------------- models
def _insert(db: Database, design_id: int, profile_id=None, **kw):
    defaults = dict(
        title=f"m{design_id}",
        slug="s",
        url="u",
        filename=f"{design_id}.3mf",
        file_path=f"/x/{design_id}.3mf",
        file_size=1,
    )
    defaults.update(kw)
    return db.insert_model(design_id=design_id, profile_id=profile_id, **defaults)


def test_insert_model_and_dedup(db: Database):
    db.insert_model(
        design_id=1,
        profile_id=None,
        title="A",
        slug="a",
        url="u",
        filename="a.3mf",
        file_path="/x/a.3mf",
        file_size=1,
    )
    assert db.model_exists_any(1)
    assert db.model_exists(1, None)
    assert not db.model_exists(1, 5)
    assert not db.model_exists_any(2)


def test_model_exists_profile_semantics(db: Database):
    """profile_id participates via IS ?: NULL only matches NULL rows."""
    _insert(db, 1, profile_id=7)
    assert db.model_exists(1, 7)
    assert not db.model_exists(1, None)
    assert db.model_exists_any(1)
    _insert(db, 2, profile_id=None)
    assert db.model_exists(2, None)
    assert not db.model_exists(2, 1)
    assert db.model_exists_any(2)


def test_insert_model_upsert_keeps_backfilled_metadata(db: Database):
    """Re-download must not blank cover/label/creator (COALESCE).

    Note: title/slug/url/filename are NOT in the ON CONFLICT SET clause —
    a re-download keeps the original title and only refreshes file/size/
    status. That's intentional: the first download's snapshot is stable.
    """
    _insert(db, 1, cover_url="https://c/1", collection_title="Coll", creator="Zed")
    db.insert_model(
        design_id=1,
        profile_id=None,
        title="A2",
        slug="a",
        url="u",
        filename="a.3mf",
        file_path="/y/a.3mf",
        file_size=9,
        cover_url=None,
        collection_title=None,
        creator=None,
    )
    rows = db.list_models()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "m1"  # preserved (not in SET clause)
    assert row["file_path"] == "/y/a.3mf"  # overwritten fields
    assert row["file_size"] == 9
    assert row["cover_url"] == "https://c/1"  # COALESCE-protected
    assert row["collection_title"] == "Coll"
    assert row["creator"] == "Zed"


def test_insert_model_fills_null_metadata_on_conflict(db: Database):
    """A re-download WITH metadata fills in what the first row lacked."""
    _insert(db, 1)
    db.insert_model(
        design_id=1,
        profile_id=None,
        title="A",
        slug="a",
        url="u",
        filename="a.3mf",
        file_path="/x/a.3mf",
        file_size=1,
        cover_url="https://c/1",
        creator="Zed",
    )
    row = db.list_models()[0]
    assert row["cover_url"] == "https://c/1"
    assert row["creator"] == "Zed"


def test_list_models_filters_and_pagination(db: Database):
    for did in range(10):
        _insert(
            db,
            did,
            collection_id=42 if did % 2 == 0 else None,
            collection_title="Even" if did % 2 == 0 else None,
        )
    assert db.count_models() == 10
    assert db.count_models(collection_id=42) == 5
    assert db.count_models(no_collection=True) == 5
    p1 = db.list_models(limit=4, offset=0)
    p2 = db.list_models(limit=4, offset=4)
    ids1 = {m["design_id"] for m in p1}
    ids2 = {m["design_id"] for m in p2}
    assert not ids1 & ids2
    assert p1[0]["created_at"] >= p2[0]["created_at"]  # newest first
    labels = {lbl["label"] for lbl in db.model_labels()}
    assert "Even" in labels
    assert any(
        lbl["label"] == "Manual download" and lbl["collection_id"] is None
        for lbl in db.model_labels()
    )


def test_update_model_status(db: Database):
    row_id = _insert(db, 1)
    db.update_model_status(row_id, "failed", "boom")
    row = db.list_models()[0]
    assert row["status"] == "failed"
    assert row["error"] == "boom"


def test_find_model_path_by_cover(db: Database):
    assert db.find_model_path_by_cover("https://c/none") is None
    _insert(db, 1, cover_url="https://c/1")
    assert db.find_model_path_by_cover("https://c/1") == "/x/1.3mf"


# ------------------------------------------------------ metadata backfill
def test_models_missing_meta_criteria(db: Database, tmp_path):
    # NULL cover or creator -> missing
    _insert(db, 1)
    _insert(db, 2, cover_url="", creator=None)
    _insert(db, 3, cover_url=None, creator="")
    # complete row, and its cover.webp exists on disk -> not missing
    dl_dir = tmp_path / "dl4"
    dl_dir.mkdir()
    (dl_dir / "cover.webp").write_bytes(b"webp")
    _insert(
        db, 4, cover_url="https://c/4", creator="Zed", file_path=str(dl_dir / "4.3mf")
    )
    missing = {r["design_id"] for r in db.models_missing_meta()}
    assert missing == {1, 2, 3}


def test_models_missing_meta_since_watermark(db: Database):
    _insert(db, 100)
    db.mark_meta_scan(clean=True)
    scan_at, clean = db.meta_scan_state()
    assert clean is True and scan_at is not None
    # nothing changed -> incremental pass empty
    assert db.models_missing_meta(since=scan_at) == []
    # a NEW download (updated_at > watermark) re-qualifies
    _insert(db, 101)
    new = db.models_missing_meta(since=scan_at)
    assert [r["design_id"] for r in new] == [101]
    # dirty scan clears the watermark
    db.mark_meta_scan(clean=False)
    scan_at, clean = db.meta_scan_state()
    assert clean is False
    # and the full pass still finds everything missing
    assert {r["design_id"] for r in db.models_missing_meta()} == {100, 101}


# ------------------------------------------------------------ collections
def test_collection_lifecycle(db: Database):
    assert db.get_collection(1) is None
    db.upsert_collection(1, "Cats", "https://mw/collections/1", 60)
    coll = db.get_collection(1)
    assert coll["title"] == "Cats"
    assert coll["enabled"] == 1
    assert coll["last_sync_at"] is None
    # upsert refreshes title/interval, keeps sync state
    db.record_sync(1, "ok", 5)
    db.upsert_collection(1, "Cats!", "https://mw/collections/1", 120)
    coll = db.get_collection(1)
    assert coll["title"] == "Cats!"
    assert coll["sync_interval_minutes"] == 120
    assert coll["last_sync_new"] == 5  # untouched by upsert
    assert len(db.list_collections()) == 1
    db.set_collection_enabled(1, False)
    assert db.get_collection(1)["enabled"] == 0
    assert db.due_collections(utcnow()) == []
    db.set_collection_enabled(1, True)
    assert db.get_collection(1)["collection_id"] == 1
    db.delete_collection(1)
    assert db.get_collection(1) is None


def test_due_collections_interval_math(db: Database):
    db.upsert_collection(1, "A", "u", 60)
    # never synced -> due immediately
    due = db.due_collections(utcnow())
    assert [c["collection_id"] for c in due] == [1]
    # just synced -> not due until the interval passes
    db.record_sync(1, "ok", 0)
    assert db.due_collections(utcnow()) == []
    # backdate last_sync by 2 hours -> due again for a 60-min interval
    with db.connect() as conn:
        conn.execute(
            "UPDATE collections SET last_sync_at = datetime('now', '-2 hours') WHERE collection_id = 1"
        )
    due = db.due_collections(utcnow())
    assert [c["collection_id"] for c in due] == [1]


def test_delete_models_only_target_collection(db: Database):
    _insert(db, 1, collection_id=10, collection_title="Ten")
    _insert(db, 2, collection_id=20, collection_title="Twenty")
    assert db.delete_models(10) == 1
    assert db.count_models(collection_id=20) == 1
    assert db.count_models() == 1


# ------------------------------------------------- remote collections cache
def _rc(cid, count, ids, is_default=False):
    return {
        "collection_id": cid,
        "title": f"T{cid}",
        "slug": f"t{cid}",
        "design_count": count,
        "is_default": is_default,
        "design_ids": ids,
    }


def test_remote_collections_replace_and_annotate(db: Database):
    _insert(db, 11)  # design 11 downloaded
    _insert(db, 13)
    db.replace_remote_collections(
        [
            _rc(1, 3, [11, 12, 13]),
            _rc(2, 2, [14, 15]),
            _rc(3, 0, []),
        ]
    )
    rows = db.remote_collections()
    by_id = {r["collection_id"]: r for r in rows}
    assert by_id[1]["checked_ids"] == [11, 13]  # in collection order
    assert by_id[1]["downloaded_count"] == 2
    assert by_id[1]["downloaded"] is False  # 2/3
    assert by_id[2]["downloaded_count"] == 0
    assert by_id[2]["checked_ids"] == []
    assert by_id[3]["downloaded"] is False  # empty collection isn't "done"
    assert by_id[3]["design_ids"] == []
    assert db.remote_collections_fetched_at() is not None


def test_remote_collections_full_match(db: Database):
    for d in (1, 2, 3):
        _insert(db, d)
    db.replace_remote_collections([_rc(1, 3, [1, 2, 3])])
    row = db.remote_collections()[0]
    assert row["downloaded"] is True
    assert row["downloaded_count"] == 3


def test_remote_collections_replace_vanishes_old(db: Database):
    db.replace_remote_collections([_rc(1, 1, [1])])
    db.replace_remote_collections([_rc(2, 1, [2])])
    assert [r["collection_id"] for r in db.remote_collections()] == [2]


def test_remote_collections_shared_design_across_lists(db: Database):
    _insert(db, 5)
    db.replace_remote_collections([_rc(1, 2, [5, 6]), _rc(2, 2, [5, 7])])
    by_id = {r["collection_id"]: r for r in db.remote_collections()}
    assert by_id[1]["checked_ids"] == [5]
    assert by_id[2]["checked_ids"] == [5]


def test_remote_collections_corrupt_json_tolerated(db: Database):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO remote_collections(collection_id, title, slug, design_count, is_default, design_ids, fetched_at)"
            " VALUES(9, 'X', 'x', 2, 0, 'not-json', '2026-01-01')"
        )
    rows = db.remote_collections()
    assert len(rows) == 1
    assert rows[0]["design_ids"] == []
    assert rows[0]["downloaded"] is False


def test_replace_remote_collections_ignores_followed_collections(db: Database):
    """The cache table is display-only: following still works off `collections`."""
    db.upsert_collection(1, "Cats", "u", 60)
    db.replace_remote_collections([_rc(1, 1, [1])])
    assert db.get_collection(1) is not None
    assert db.get_collection(1)["title"] == "Cats"
