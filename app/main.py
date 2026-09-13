"""Bambu Downloader — FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import routes
from .config import settings
from .db import Database
from .downloader import DownloadManager
from .makerworld import MakerWorldError, get_client, invalidate_shared_clients, release_client
from .scheduler import SyncScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bambu_downloader")


def _probe_writable(path: Path, env_name: str) -> None:
    """Fail fast with actionable advice when a bind mount isn't writable.

    Rootless podman maps container UIDs into a subuid range, so bind-mounted
    dirs owned by the host user can appear read-only in the container — better
    to say that at startup than as a crash mid-download.
    """
    probe = path / f".write-probe-{os.getpid()}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as e:
        raise RuntimeError(
            f"{env_name} ({path}) is not writable by the container user "
            f"(uid {os.getuid()}; {e}). With rootless podman use "
            "--userns=keep-id, or 'podman unshare chown -R 1000:1000 <dir>'."
        ) from e


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown: build the app's singletons and tear them down cleanly.

    Startup probes the bind mounts for writability (fail fast with podman
    advice), opens the database, starts the collection scheduler and kicks
    off the metadata backfill in the background. Shutdown cancels the
    backfill and scheduler so in-flight syncs stop and SQLite checkpoints —
    uvicorn runs this on SIGTERM/SIGINT (podman stop / compose stop).
    """
    Path(settings.download_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    _probe_writable(Path(settings.download_dir), "BND_DOWNLOAD_DIR")
    _probe_writable(Path(settings.data_dir), "BND_DATA_DIR")
    database = Database(settings.db_path)
    manager = DownloadManager(database)
    scheduler = SyncScheduler(database, manager)
    routes.init(database, manager, scheduler)
    scheduler.start()
    # Fetch missing metadata (cover, creator) for pre-existing models in
    # the background — must not block startup.
    meta_task = asyncio.create_task(manager.backfill_metadata())
    logger.info("Bambu Downloader ready — downloads: %s, db: %s", settings.download_dir, settings.db_path)
    try:
        yield
    finally:
        # Graceful shutdown: cancel the scheduler + in-flight syncs so
        # SQLite checkpoints cleanly and no .part files linger. Uvicorn
        # runs this on SIGTERM/SIGINT (podman stop sends SIGTERM).
        logger.info("Shutting down — stopping scheduler…")
        meta_task.cancel()
        await scheduler.stop()
        # Release every pooled httpx connection so sockets don't outlive
        # the loop and trip 'Event loop is closed' warnings on shutdown.
        await invalidate_shared_clients()
        logger.info("Shutdown complete")


app = FastAPI(title="Bambu Downloader", lifespan=lifespan)

app.include_router(routes.router)


# ------------------------------------------------------------- static files
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Thumbnails: served from disk when possible (each model folder has a
# cover.webp), otherwise fetched from the MakerWorld CDN via the /thumb
# proxy — the frontend can't hotlink the CDN directly (CORS).


def _sniff_media(blob: bytes) -> str:
    """Covers come back as webp, png, or jpeg — read the magic bytes."""
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def _thumb_etag(blob: bytes, mtime_ns: int | None = None) -> str:
    """Strong ETag for a cover response (mtime for disk copies, size+width
    hash for CDN fetches) so revalidating browsers can get a 304."""
    import hashlib

    basis = f"{mtime_ns or 0}:{len(blob)}".encode()
    return '"' + hashlib.sha1(basis).hexdigest()[:16] + '"'


@app.get("/thumb")
async def thumbnail(url: str, w: int = 512, if_none_match: str | None = Header(default=None)):
    """Serve a model's cover thumbnail.

    Covers are saved as cover.webp next to each model file, so this first
    serves the local copy (no CDN round-trip, works offline). On a miss it
    fetches the resized image from MakerWorld's CDN (Aliyun OSS resize — a
    4MB PNG becomes a ~22KB WebP) and persists it next to the model so the
    next request is local. ETag/If-None-Match: browsers revalidating after
    their 24h cache expiry get a 304 instead of re-downloading.
    """
    from urllib.parse import urlparse

    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="https URL required")
    if urlparse(url).hostname not in ("makerworld.bblmw.com", "public-cdn.bblmw.com", "makerworld.com"):
        raise HTTPException(status_code=400, detail="host not allowed")
    w = max(64, min(w, 1920))  # clamp to sane sizes

    def _not_modified(etag: str) -> Response:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "public, max-age=86400"},
        )

    def _matches(etag: str, header: str | None) -> bool:
        if not header:
            return False
        return any(candidate.strip() == etag or candidate.strip() == "*" for candidate in header.split(","))

    model_file = routes.db.find_model_path_by_cover(url)
    local = (Path(model_file).parent / "cover.webp") if model_file else None
    if local and local.exists():
        blob = local.read_bytes()
        etag = _thumb_etag(blob, local.stat().st_mtime_ns)
        if _matches(etag, if_none_match):
            return _not_modified(etag)
        return Response(
            content=blob,
            media_type=_sniff_media(blob),
            headers={"Cache-Control": "public, max-age=86400", "ETag": etag},
        )

    client = get_client()  # anonymous pooled — covers are fetched via CDN URL
    try:
        blob = await client.fetch_thumbnail(url, width=w)
    except MakerWorldError:
        raise HTTPException(status_code=502, detail="upstream fetch failed")
    finally:
        await release_client(client)
    # Persist next to the model so future requests (and the file manager)
    # don't need the CDN.
    if local and blob:
        try:
            local.write_bytes(blob)
        except OSError:
            logger.warning("could not persist cover at %s", local)
    etag = _thumb_etag(blob)
    return Response(
        content=blob,
        media_type=_sniff_media(blob),
        headers={"Cache-Control": "public, max-age=86400", "ETag": etag},
    )


@app.get("/manifest.webmanifest")
async def manifest():
    """Serve the PWA manifest (installability + Android share target)."""
    return FileResponse(_static_dir / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    """Serve the service worker with no-store cache headers.

    The service worker itself must NEVER be cached by the browser — a stale
    SW keeps serving an old cached shell. Standard practice per MDN.
    """
    return FileResponse(
        _static_dir / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/")
async def index():
    """Serve the app shell with no-store headers so UI updates land promptly."""
    return FileResponse(
        _static_dir / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/favicon.ico")
async def favicon():
    """Serve the app icon as the browser favicon."""
    p = _static_dir / "icons" / "icon-192.png"
    if p.exists():
        return FileResponse(p)
    raise HTTPException(status_code=404)