"""SQLite persistence for downloads, auth token, and collection sync state."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id INTEGER NOT NULL,
    profile_id INTEGER,
    collection_id INTEGER,
    title TEXT NOT NULL,
    slug TEXT,
    url TEXT,
    cover_url TEXT,
    collection_title TEXT,
    creator TEXT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER,
    status TEXT NOT NULL DEFAULT 'completed',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(design_id, profile_id)
);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL UNIQUE,
    title TEXT,
    url TEXT NOT NULL,
    sync_interval_minutes INTEGER NOT NULL DEFAULT 360,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_sync_at TEXT,
    last_sync_status TEXT,
    last_sync_new INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


# Label shown for models downloaded directly by URL (not via a collection).
MANUAL_DOWNLOAD_LABEL = "Manual download"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin wrapper over sqlite3 with WAL mode for concurrent access."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        try:
            with self.connect() as conn:
                conn.executescript(DB_SCHEMA)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "readonly" in msg or "unable to open" in msg:
                raise RuntimeError(
                    f"Cannot write to the database at {db_path} (running as uid {os.getuid()}). "
                    f"The data volume is not writable by the container user (sqlite said: {e}). Fixes:\n"
                    "  podman run --userns=keep-id ...   (rootless podman: your host uid appears\n"
                    "                                     inside the container, bind mounts work)\n"
                    "  podman unshare chown -R 1000:1000 ./data ./downloads\n"
                    "                                     (alternative: give the dirs to your subuid range)\n"
                    "  docker: chown -R 1000:1000 ./data ./downloads on the host"
                ) from e
            raise
        # Lightweight migrations for DBs created before a column existed.
        # "duplicate column name" means the migration already ran — skip.
        with self.connect() as conn:
            for stmt in (
                "ALTER TABLE models ADD COLUMN cover_url TEXT",
                "ALTER TABLE models ADD COLUMN collection_title TEXT",
                "ALTER TABLE models ADD COLUMN creator TEXT",
            ):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            # Label migration: snapshot the owning collection's title onto
            # models downloaded before labels existed (idempotent — only
            # fills empty titles, so renamed collections aren't clobbered).
            conn.execute(
                """UPDATE models SET collection_title = (
                       SELECT c.title FROM collections c
                       WHERE c.collection_id = models.collection_id)
                   WHERE collection_id IS NOT NULL
                     AND (collection_title IS NULL OR collection_title = '')"""
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- meta (key/value for token etc.) ----
    def get_meta(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_meta(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    # ---- models ----
    def model_exists(self, design_id: int, profile_id: int | None) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM models WHERE design_id = ? AND profile_id IS ?",
                (design_id, profile_id),
            ).fetchone()
            return row is not None

    def insert_model(
        self,
        design_id: int,
        profile_id: int | None,
        title: str,
        slug: str | None,
        url: str | None,
        filename: str,
        file_path: str,
        file_size: int | None,
        collection_id: int | None = None,
        status: str = "completed",
        error: str | None = None,
        cover_url: str | None = None,
        collection_title: str | None = None,
        creator: str | None = None,
    ) -> int:
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO models(design_id, profile_id, collection_id, title, slug, url,
                   cover_url, collection_title, creator, filename, file_path, file_size,
                   status, error, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(design_id, profile_id) DO UPDATE SET
                     file_path=excluded.file_path, file_size=excluded.file_size,
                     status=excluded.status, error=excluded.error,
                     cover_url=COALESCE(excluded.cover_url, models.cover_url),
                     collection_title=COALESCE(excluded.collection_title, models.collection_title),
                     creator=COALESCE(excluded.creator, models.creator),
                     updated_at=excluded.updated_at""",
                (
                    design_id,
                    profile_id,
                    collection_id,
                    title,
                    slug,
                    url,
                    cover_url,
                    collection_title,
                    creator,
                    filename,
                    file_path,
                    file_size,
                    status,
                    error,
                    now,
                    now,
                ),
            )
            return cur.lastrowid or 0

    def update_model_status(self, model_row_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE models SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error, utcnow(), model_row_id),
            )

    # ---- metadata backfill ----
    def models_missing_meta(self) -> list[dict[str, Any]]:
        """Rows needing a metadata pass: cover/creator never fetched, or the
        cover.webp file is missing next to the model file.
        NULL = never checked; "" = checked, none exists (those rows only
        re-qualify through the file check when a cover exists)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT design_id, profile_id, file_path, cover_url, creator FROM models"
            ).fetchall()
        missing: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            needs = row["cover_url"] is None or row["creator"] is None
            if not needs and row["file_path"] and row["cover_url"]:
                needs = not (Path(row["file_path"]).parent / "cover.webp").exists()
            if needs:
                missing.append(row)
        return missing

    def find_model_path_by_cover(self, cover_url: str) -> str | None:
        """Locate the model file that owns this cover URL — lets the /thumb
        proxy serve the on-disk cover.webp instead of the CDN."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT file_path FROM models WHERE cover_url = ? LIMIT 1", (cover_url,)
            ).fetchone()
            return row["file_path"] if row else None

    def set_model_meta(
        self,
        design_id: int,
        profile_id: int | None,
        cover_url: str | None,  # "" = checked, no cover; None = leave alone
        creator: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE models SET
                     cover_url = COALESCE(?, cover_url),
                     creator = COALESCE(?, creator),
                     updated_at = ?
                   WHERE design_id = ? AND profile_id IS ?""",
                (cover_url, creator, utcnow(), design_id, profile_id),
            )

    @staticmethod
    def _model_filters(
        collection_id: int | None = None,
        no_collection: bool = False,
        label: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Shared WHERE builder so list/count totals always match the grid.
        Filters combine with AND."""
        where: list[str] = []
        params: list[Any] = []
        if collection_id is not None:
            where.append("collection_id = ?")
            params.append(collection_id)
        if no_collection:
            where.append("collection_id IS NULL")
        if label is not None:
            if label == MANUAL_DOWNLOAD_LABEL:
                where.append("collection_id IS NULL")
            else:
                where.append("collection_title = ?")
                params.append(label)
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def list_models(
        self,
        collection_id: int | None = None,
        no_collection: bool = False,
        label: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where_sql, params = self._model_filters(collection_id, no_collection, label)
        query = f"SELECT * FROM models{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params = params + [limit, offset]
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def count_models(
        self,
        collection_id: int | None = None,
        no_collection: bool = False,
        label: str | None = None,
    ) -> int:
        where_sql, params = self._model_filters(collection_id, no_collection, label)
        query = f"SELECT COUNT(*) as c FROM models{where_sql}"
        with self.connect() as conn:
            return conn.execute(query, params).fetchone()["c"]

    # ---- labels ----
    def model_labels(self) -> list[dict[str, Any]]:
        """Origin labels with counts, for the Library filter bar.

        The label is the snapshot of the collection title the model was
        downloaded through; models with no collection are 'Manual download'.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT collection_id, collection_title, COUNT(*) AS n
                   FROM models GROUP BY collection_id, collection_title"""
            ).fetchall()
        labels: list[dict[str, Any]] = []
        for r in rows:
            if r["collection_id"] is None:
                labels.append(
                    {"label": MANUAL_DOWNLOAD_LABEL, "count": r["n"], "collection_id": None}
                )
            else:
                name = r["collection_title"] or f"Collection #{r['collection_id']}"
                labels.append(
                    {"label": name, "count": r["n"], "collection_id": r["collection_id"]}
                )
        labels.sort(key=lambda x: -x["count"])
        return labels

    # ---- collections ----
    def upsert_collection(self, collection_id: int, title: str, url: str, interval: int) -> int:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO collections(collection_id, title, url, sync_interval_minutes, created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(collection_id) DO UPDATE SET
                     title=excluded.title, url=excluded.url, sync_interval_minutes=excluded.sync_interval_minutes""",
                (collection_id, title, url, interval, now),
            )
            row = conn.execute(
                "SELECT id FROM collections WHERE collection_id = ?", (collection_id,)
            ).fetchone()
            return row["id"]

    def list_collections(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM collections ORDER BY created_at DESC").fetchall()]

    def get_collection(self, collection_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM collections WHERE collection_id = ?", (collection_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_collection_enabled(self, collection_id: int, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE collections SET enabled = ? WHERE collection_id = ?",
                (1 if enabled else 0, collection_id),
            )

    def set_collection_interval(self, collection_id: int, minutes: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE collections SET sync_interval_minutes = ? WHERE collection_id = ?",
                (minutes, collection_id),
            )

    def record_sync(self, collection_id: int, status: str, new_count: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE collections SET last_sync_at = ?, last_sync_status = ?, last_sync_new = ? WHERE collection_id = ?",
                (utcnow(), status, new_count, collection_id),
            )

    def delete_collection(self, collection_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM collections WHERE collection_id = ?", (collection_id,))

    def delete_models(self, collection_id: int) -> int:
        """Remove all library rows belonging to a collection (used by unfollow
        with file deletion). Returns the number of rows removed."""
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM models WHERE collection_id = ?", (collection_id,))
            return cur.rowcount or 0

    def due_collections(self, now_iso: str) -> list[dict[str, Any]]:
        """Collections whose interval has elapsed since last sync (or never synced), and enabled."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM collections WHERE enabled = 1 AND (
                     last_sync_at IS NULL
                     OR (julianday(?) - julianday(last_sync_at)) * 1440.0 >= sync_interval_minutes
                   )""",
                (now_iso,),
            ).fetchall()
            return [dict(r) for r in rows]