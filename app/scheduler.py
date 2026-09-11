"""Background scheduler that periodically syncs registered collections."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .db import Database, utcnow
from .downloader import DownloadManager, add_event
from .makerworld import AuthRequiredError, CaptchaError, MakerWorldError

logger = logging.getLogger(__name__)


class SyncScheduler:
    """Polls due collections and syncs them, one at a time."""

    def __init__(self, db: Database, manager: DownloadManager) -> None:
        self.db = db
        self.manager = manager
        self._task: asyncio.Task[None] | None = None
        self._running = False
        # In-flight syncs for UI feedback.
        self.active: dict[int, str] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="sync-scheduler")

    async def stop(self) -> None:
        """Cancel the scheduler and any manual sync in flight (graceful shutdown)."""
        self._running = False
        tasks: list[asyncio.Task[None]] = []
        if self._task:
            tasks.append(self._task)
            self._task = None
        for task in list(_manual_tasks.values()):
            if not task.done():
                tasks.append(task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.active.clear()
        _manual_tasks.clear()

    async def _loop(self) -> None:
        logger.info("Collection sync scheduler started")
        while self._running:
            try:
                due = self.db.due_collections(utcnow())
                for coll in due:
                    cid = coll["collection_id"]
                    if not self._running:
                        break
                    if cid in self.active:
                        continue
                    self.active[cid] = "syncing"
                    try:
                        await self.manager.sync_collection(cid)
                    except AuthRequiredError:
                        # Shouldn't normally happen (sync_collection handles it
                        # internally), but keep the safety net for listing-stage
                        # auth failures (before any download attempt).
                        await add_event("error", f"Collection {cid} sync skipped — not signed in to MakerWorld")
                        self.db.record_sync(cid, "auth-required", 0)
                    except CaptchaError:
                        # Same: safety net for listing-stage challenges.
                        await add_event("error", f"Collection {cid} sync skipped — rate-limited (CAPTCHA)")
                        self.db.record_sync(cid, "captcha", 0)
                    except asyncio.CancelledError:
                        self.active.pop(cid, None)
                        raise
                    except MakerWorldError as e:
                        await add_event("error", f"Collection {cid} sync failed — {e}")
                        self.db.record_sync(cid, "error", 0)
                    finally:
                        self.active.pop(cid, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler iteration failed")
            # Poll interval is configurable; a small floor avoids busy-looping.
            await asyncio.sleep(settings.scheduler_interval_seconds)

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "active": dict(self.active),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


# Manual trigger helper used by API routes: run a sync now in the background.
_manual_tasks: dict[int, asyncio.Task[None]] = {}


def trigger_sync(manager: DownloadManager, collection_id: int) -> bool:
    """Fire a background sync for a collection. Returns False if one is already running."""
    existing = _manual_tasks.get(collection_id)
    if existing and not existing.done():
        return False

    async def run() -> None:
        try:
            await manager.sync_collection(collection_id)
        except MakerWorldError as e:
            await add_event("error", f"Manual sync of collection {collection_id} failed — {e}")

    _manual_tasks[collection_id] = asyncio.get_running_loop().create_task(run(), name=f"sync-{collection_id}")
    return True