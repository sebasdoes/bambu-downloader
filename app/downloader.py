"""Download manager: resolves models to 3MF URLs and saves them without duplicates."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .config import settings
from .db import Database
from .makerworld import (
    AuthExpiredError,
    AuthRequiredError,
    CaptchaError,
    MakerWorldClient,
    MakerWorldError,
    NotFoundError,
    get_client,
    invalidate_shared_clients,
    parse_model_url,
    release_client,
)

logger = logging.getLogger(__name__)


def _token_cache_reset() -> None:
    """Clear the routes module's token-validation cache after a refresh.

    Imported lazily: downloader must not depend on routes (routes depends
    on downloader). The import is stable at runtime — main.py imports both
    before serving traffic.
    """
    from . import routes

    try:
        routes._token_cache.clear()
    except AttributeError:
        pass


# Progress events for the UI (recent activity log kept in memory).
_events: list[dict[str, Any]] = []
_events_lock = asyncio.Lock()
_MAX_EVENTS = 200


async def add_event(kind: str, message: str, **extra: Any) -> None:
    """Append an activity-log event for the UI (in-memory, capped ring buffer).

    kind is 'download' / 'sync' / 'error'; extra kwargs become extra fields.
    """
    async with _events_lock:
        _events.append({"ts": time.time(), "kind": kind, "message": message, **extra})
        if len(_events) > _MAX_EVENTS:
            del _events[: len(_events) - _MAX_EVENTS]


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent events, newest first."""
    return list(reversed(_events[-limit:]))


def _slugify(text: str) -> str:
    """Turn an arbitrary title into a safe single path component.

    Lowercases, strips punctuation, collapses whitespace to single hyphens,
    and truncates to 80 chars so folder names stay filesystem- and
    file-manager-friendly.
    """
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", text)[:80] or "untitled"


def _ensure_disk_space(dest_dir: Path) -> None:
    """Refuse a download when the volume is nearly full (BND_MIN_FREE_MB).

    Checked just before the temp file is created so a full disk fails
    cleanly with a clear message instead of a truncated .3mf mid-write.
    0 disables the check (e.g. filesystems reporting bogus free space).
    """
    reserve = settings.min_free_mb
    if reserve <= 0:
        return
    try:
        free_mb = shutil.disk_usage(dest_dir).free // (1024 * 1024)
    except OSError:
        return  # stat unavailable — don't block downloads over it
    if free_mb < reserve:
        raise MakerWorldError(
            f"Downloads volume is low on space ({free_mb} MB free, {reserve} MB "
            "reserve configured via BND_MIN_FREE_MB). Free up space or set "
            "BND_MIN_FREE_MB=0 to disable this guard."
        )


async def _save_cover_file(
    client: MakerWorldClient, cover_url: str, model_dir: Path
) -> None:
    """Persist the resized cover as cover.webp next to the model file, so the
    downloads folder is browsable in any file manager and the grid can be
    served from disk. Cosmetic — never fail a download over it."""
    try:
        blob = await client.fetch_thumbnail(cover_url, width=512)
        (model_dir / "cover.webp").write_bytes(blob)
    except MakerWorldError as e:
        logger.warning("cover save failed for %s: %s", cover_url[:80], e)
    except OSError as e:
        logger.warning("cover write failed: %s", e)


class DownloadManager:
    """Coordinates downloads with the SQLite dedup store."""

    def __init__(self, db: Database) -> None:
        """Store the DB handle and create the download concurrency limiter."""
        self.db = db
        # Serialize downloads to be gentle on MakerWorld.
        self._sem = asyncio.Semaphore(2)
        # One token-refresh attempt at a time (see try_token_refresh).
        self._refresh_lock = asyncio.Lock()
        # Download queue visibility for the UI: {design_id: 'queued'|'active'}
        # (plus in-flight title for the Activity tab). Pruned in finally.
        self.downloads: dict[int, dict[str, Any]] = {}

    def queue_status(self) -> list[dict[str, Any]]:
        """Snapshot of in-flight/queued downloads for /api/status."""
        return [{"design_id": did, **info} for did, info in self.downloads.items()]

    async def backfill_metadata(self) -> None:
        """Fetch covers + creator for models downloaded before those columns
        existed (label titles are migrated in Database.__init__).

        Anonymous design lookups, one at a time with a small delay — same
        politeness rules as syncs so the anti-abuse layer stays calm. Absent
        values are stored as "" (checked) so they aren't re-fetched every boot.

        Scan watermark: after a scan that found nothing missing, later boots
        re-check only rows updated since that scan (new downloads, status
        changes) instead of statting every file in the library. A non-empty
        result clears the watermark so the next boot re-scans fully to
        verify the backfill took.
        """
        scan_at, clean = self.db.meta_scan_state()
        since = scan_at if (clean and scan_at) else None
        rows = self.db.models_missing_meta(since)
        if not rows:
            self.db.mark_meta_scan(clean=True)
            if since:
                logger.info(
                    "Metadata scan: nothing new since %s (incremental)", scan_at
                )
            return
        if since:
            logger.info(
                "Metadata scan: %d candidates updated since %s", len(rows), scan_at
            )
        else:
            logger.info("Metadata scan: full pass (%d models)", len(rows))
        # Clear the watermark up front: this pass found work, so the next
        # boot must re-scan from scratch to verify the fixes landed.
        self.db.mark_meta_scan(clean=False)
        logger.info("Backfilling metadata for %d models…", len(rows))
        client = self._client()
        try:
            for i, row in enumerate(rows):
                if i > 0 and settings.download_delay_seconds > 0:
                    await asyncio.sleep(settings.download_delay_seconds)
                design_id = int(row["design_id"])
                try:
                    design = await client.get_design(design_id)
                    cover = str(design.get("coverUrl") or "")
                    creator = str((design.get("designCreator") or {}).get("name") or "")
                    self.db.set_model_meta(design_id, row["profile_id"], cover, creator)
                    # Also (re)create the cover.webp file next to the model.
                    if cover and row.get("file_path"):
                        model_dir = Path(row["file_path"]).parent
                        if not (model_dir / "cover.webp").exists():
                            await _save_cover_file(client, cover, model_dir)
                except MakerWorldError as e:
                    logger.warning("metadata backfill for %s failed: %s", design_id, e)
        finally:
            await release_client(client)
        logger.info("Metadata backfill done")

    def _client(self) -> MakerWorldClient:
        """Return the pooled API client for the stored token + region.

        Pooled: the httpx connection pool (and its warm TLS connections) is
        shared across downloads, syncs and backfills. Never close the
        returned client — release_client() in the finally blocks does the
        right thing (keeps pooled ones warm, closes ad-hoc ones).
        """
        token = self.db.get_meta("bambu_token")
        region = self.db.get_meta("bambu_region") or "global"
        return get_client(auth_token=token, region=region)

    async def try_token_refresh(self) -> bool:
        """Try to mint a fresh access token with the stored refresh token.

        Called when Bambu rejects the current access token. On success the
        new token (+ possibly rotated refresh token) is persisted, the
        client pool is invalidated so the next call carries the new token,
        and the token-validation cache is reset. Returns True when the
        session was renewed — the caller may then retry the failed request.

        Failure (no stored refresh token / Bambu refuses / unreachable)
        leaves everything untouched: the normal AuthExpiredError path
        ("please sign in again") applies. A lock keeps concurrent syncs and
        API calls from stampeding the refresh endpoint with parallel
        attempts; losers re-read the (now current) token instead.
        """
        if not self.db.get_meta("bambu_token_refresh"):
            return False
        async with self._refresh_lock:
            # Another task may have refreshed while we waited.
            if not self.db.get_meta("bambu_token_refresh"):
                return False
            current = self.db.get_meta("bambu_token_refresh")
            client = get_client()  # anonymous: refresh is its own credential
            try:
                result = await client.refresh_access_token(current)
            finally:
                await release_client(client)
            if not result:
                # Keep the old refresh token: Bambu may accept it later
                # (e.g. transient 5xx) and wiping it here would turn a
                # maybe-recoverable session into a guaranteed sign-out.
                return False
            self.db.set_meta("bambu_token", result["access_token"])
            self.db.set_meta("bambu_token_refresh", result["refresh_token"])
            await invalidate_shared_clients()
            _token_cache_reset()
            await add_event("sync", "Access token expired — refreshed automatically")
            return True

    async def refresh_my_collections(self) -> dict[str, Any]:
        """Fetch the user's own MakerWorld collections and cache them.

        Requires a stored token (AuthRequiredError otherwise). The listing
        request itself is light (one endpoint, paginated); design ids come
        from the embedded page-1 payloads. Results land in the
        remote_collections table via replace_remote_collections; download
        checkmarks are computed at read time against the library. Returns
        {"collections": <count>, "fetched_at": iso} for the UI.
        """
        if not self.db.get_meta("bambu_token"):
            raise AuthRequiredError("Sign in to MakerWorld first (Settings → Login).")
        client = self._client()
        try:
            mine = await client.list_my_collections()
        finally:
            await release_client(client)
        self.db.replace_remote_collections(mine)
        await add_event(
            "sync",
            f"Refreshed your MakerWorld collections ({len(mine)} found)",
        )
        return {
            "collections": len(mine),
            "fetched_at": self.db.remote_collections_fetched_at(),
        }

    async def resolve_design(self, url: str) -> dict[str, Any]:
        """Preview a model URL: design metadata + plate instances, no download.

        Also reports whether the (design_id, profile_id) pair is already in
        the library so the UI can show an "already downloaded" hint.
        """
        design_id, profile_id = parse_model_url(url)
        client = self._client()
        try:
            design = await client.get_design(design_id)
            instances_env = await client.get_design_instances(design_id)
        finally:
            await release_client(client)
        instances = instances_env.get("hits") or []
        return {
            "design_id": design_id,
            "profile_id": profile_id,
            "design": design,
            "instances": instances,
            "already_downloaded": (
                self.db.model_exists(design_id, profile_id)
                if profile_id
                else self.db.model_exists_any(design_id)
            ),
        }

    async def download_model(
        self,
        url: str,
        collection_id: int | None = None,
        subfolder: str | None = None,
    ) -> dict[str, Any]:
        """Download a model by URL. Returns a summary dict.

        Dedup: keyed on (design_id, profile_id). If already present and the
        file still exists on disk, it is skipped.
        """
        design_id, profile_id = parse_model_url(url)

        # Dedup: exact (design_id, profile_id) pair when the URL pins a
        # plate, otherwise design-level — ANY plate already downloaded
        # counts, because we'd resolve to (and re-store) the same default
        # plate anyway.
        if (
            self.db.model_exists_any(design_id)
            if profile_id is None
            else self.db.model_exists(design_id, profile_id)
        ):
            return {
                "status": "exists",
                "design_id": design_id,
                "profile_id": profile_id,
            }

        self.downloads[design_id] = {"state": "queued"}
        try:
            async with self._sem:
                self.downloads[design_id] = {"state": "active"}
                client = self._client()
                try:
                    design = await client.get_design(design_id)
                    title = str(design.get("title") or f"model-{design_id}")
                    slug = design.get("slug")
                    cover_url = design.get("coverUrl")
                    creator = (design.get("designCreator") or {}).get("name")

                    instances_env = await client.get_design_instances(design_id)
                    instances = instances_env.get("hits") or []

                    # Pick target profile/instance. NOTE: a URL's #profileId-N
                    # fragment refers to the instance's profileId field (the
                    # "plate" in Bambu Studio), not the instance row id.
                    instance = None
                    if profile_id:
                        for inst in instances:
                            if int(inst.get("profileId") or 0) == profile_id:
                                instance = inst
                                break
                    if instance is None and instances:
                        instance = instances[0]

                    # --- download URL resolution --------------------------------
                    # Primary: Bambu iot-service profile download (works for all
                    # models incl. new ones; on api.bambulab.com, no CAPTCHA
                    # pressure). Legacy makerworld.com endpoints are dead for
                    # newer designs (HTTP 400) but kept as fallbacks for old ones.
                    download_url: str | None = None
                    filename_hint: str | None = None
                    alphanumeric_model_id = design.get("modelId")
                    inst_profile_id = (
                        int(instance.get("profileId") or 0) if instance else 0
                    )
                    if (
                        self.db.get_meta("bambu_token")
                        and inst_profile_id
                        and alphanumeric_model_id
                    ):
                        try:
                            manifest = await client.get_profile_download(
                                inst_profile_id, str(alphanumeric_model_id)
                            )
                            download_url = _find_url(manifest)
                            if (
                                isinstance(manifest.get("name"), str)
                                and manifest["name"]
                            ):
                                filename_hint = manifest["name"]
                        except (AuthRequiredError, AuthExpiredError):
                            raise
                        except CaptchaError:
                            raise
                        except (NotFoundError, MakerWorldError) as e:
                            logger.warning(
                                "profile download failed for %s (profile %s): %s",
                                design_id,
                                inst_profile_id,
                                e,
                            )
                    # Fallback 1: legacy per-instance endpoint (old models only).
                    if not download_url and instance:
                        inst_id = int(instance.get("id") or 0)
                        try:
                            dl = await client.get_instance_download(inst_id)
                            download_url = _find_url(dl)
                        except CaptchaError:
                            raise
                        except (AuthRequiredError, MakerWorldError) as e:
                            logger.warning(
                                "instance download failed for %s: %s", design_id, e
                            )
                    # Fallback 2: legacy design-level endpoint (old models only).
                    if not download_url:
                        try:
                            dl = await client.get_design_model_download(design_id)
                            download_url = _find_url(dl)
                        except AuthRequiredError:
                            # Preserve the auth signal so syncs abort early instead
                            # of failing per-model 68 times.
                            raise
                        except CaptchaError:
                            raise
                        except MakerWorldError as e:
                            raise MakerWorldError(
                                f"Could not get a download URL — you are probably not signed in. ({e})"
                            ) from e
                    if not download_url:
                        raise MakerWorldError(
                            "MakerWorld did not return a download URL for this model."
                        )

                    # Build destination: downloads/<collection>/<model>/
                    parts: list[str] = []
                    if subfolder:
                        parts.append(_slugify(subfolder))
                    parts.append(_slugify(f"{design_id}-{title}"))
                    dest_dir = Path(settings.download_dir).joinpath(*parts)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    _ensure_disk_space(dest_dir)

                    # Unique temp name so concurrent downloads can't collide.
                    tmp = dest_dir / f".{design_id}-{time.monotonic_ns()}.part"
                    try:
                        size, remote_name = await client.download_file(
                            download_url, tmp
                        )
                        final_name = filename_hint or remote_name or f"{design_id}.3mf"
                        dest = dest_dir / _safe_filename(final_name)
                        tmp.replace(dest)
                    finally:
                        # Remove the partial file if the download failed midway.
                        tmp.unlink(missing_ok=True)

                    # Save the cover next to the model file.
                    if cover_url:
                        await _save_cover_file(client, str(cover_url), dest_dir)

                    # Dedup identity: the profileId (plate) from the URL fragment,
                    # else the instance's profileId, else the instance row id.
                    stored_profile_id = profile_id or (
                        int(instance.get("profileId") or 0)
                        or int(instance.get("id") or 0)
                        if instance
                        else None
                    )
                    # Snapshot the collection title as the model's origin label —
                    # it must survive the collection being unfollowed later.
                    coll_title: str | None = None
                    if collection_id:
                        coll = self.db.get_collection(collection_id)
                        coll_title = str((coll or {}).get("title") or "") or None
                    self.db.insert_model(
                        design_id=design_id,
                        profile_id=stored_profile_id,
                        title=title,
                        slug=slug,
                        url=url,
                        filename=dest.name,
                        file_path=str(dest),
                        file_size=size,
                        collection_id=collection_id,
                        cover_url=str(cover_url) if cover_url else None,
                        collection_title=coll_title,
                        creator=str(creator) if creator else None,
                    )
                    origin = (
                        f" from “{coll_title}”" if coll_title else " (manual download)"
                    )
                    await add_event(
                        "download",
                        f"Downloaded “{title}”{origin} ({size // 1024} KB)",
                        design_id=design_id,
                        collection_id=collection_id,
                        collection_title=coll_title,
                    )
                    return {
                        "status": "downloaded",
                        "design_id": design_id,
                        "profile_id": stored_profile_id,
                        "title": title,
                        "path": str(dest),
                        "size": size,
                    }
                finally:
                    await release_client(client)
        finally:
            # Queue bookkeeping: whatever the outcome, this design's slot is
            # done (the UI re-reads the snapshot per poll).
            self.downloads.pop(design_id, None)

    async def sync_collection(self, collection_id: int) -> dict[str, Any]:
        """Download all new models from a collection; returns a summary dict.

        Already-downloaded designs are skipped (dedup by design_id), a
        politeness delay is kept between downloads, and the whole run stops
        early on auth-required or CAPTCHA aborts — partial progress is
        recorded via record_sync. The returned dict carries new/total
        counts, per-model errors, and a status string ('ok', 'partial …',
        'auth-required' or 'captcha').
        """
        coll = self.db.get_collection(collection_id)
        if not coll:
            raise MakerWorldError("Collection is not registered")
        client = self._client()
        try:
            info = await client.get_collection_info(collection_id)
            title = str(info.get("title") or f"collection-{collection_id}")
            designs = await client.list_collection_designs(collection_id)
        finally:
            await release_client(client)

        self.db.upsert_collection(
            collection_id, title, coll["url"], coll["sync_interval_minutes"]
        )

        plates_mode = coll.get("plates_mode") or "default"
        new_count = 0
        errors: list[str] = []
        attempted = 0
        aborted: str | None = None
        for hit in designs:
            design_id = int(hit.get("id") or 0)
            if not design_id:
                continue
            if plates_mode == "all":
                # Enumerate every plate; already-stored (design, plate) pairs
                # are skipped via model_exists, so the instances lookup only
                # costs a round trip for designs that are already known.
                if self.db.model_exists_any(design_id):
                    try:
                        instances = await self._design_instances(design_id)
                    except MakerWorldError as e:
                        errors.append(f"model {design_id}: {e}")
                        await add_event(
                            "error",
                            f"Collection {collection_id}: model {design_id} instances failed — {e}",
                        )
                        continue
                    plates = [
                        int(i.get("profileId") or 0) or int(i.get("id") or 0)
                        for i in instances
                    ]
                    plates = sorted({p for p in plates if p})
                    missing = [
                        p for p in plates if not self.db.model_exists(design_id, p)
                    ]
                    targets = [f"#profileId-{p}" for p in missing]
                    # An all-plates design with NO enumerated plates at all
                    # (unusual API shape): leave it to the default-plate path.
                    if not plates and not self.db.design_fully_pinned(design_id, []):
                        targets = [""]
                else:
                    targets = [""]
            else:
                targets = [""] if not self.db.model_exists_any(design_id) else []
            for fragment in targets:
                plate = int(fragment.rsplit("-", 1)[-1]) if fragment else None
                url = f"https://makerworld.com/en/models/{design_id}{fragment}"
                # Politeness: keep a gap between downloads (and retries) so
                # MakerWorld's anti-abuse layer (HTTP 418) doesn't flag the
                # burst. Skipped-on-first-attempt so single downloads are instant.
                attempted += 1
                if attempted > 1 and settings.download_delay_seconds > 0:
                    await asyncio.sleep(settings.download_delay_seconds)
                try:
                    result = await self.download_model(
                        url,
                        collection_id=collection_id,
                        subfolder=title,
                    )
                    if result["status"] == "downloaded":
                        new_count += 1
                except AuthRequiredError:
                    # The session may simply have expired: try one silent
                    # refresh with the stored refresh token. Success -> retry
                    # THIS model and continue the sync; failure -> stop here
                    # (progress so far is kept and recorded below).
                    if await self.try_token_refresh():
                        retried = await self.download_model(
                            url,
                            collection_id=collection_id,
                            subfolder=title,
                        )
                        if retried["status"] == "downloaded":
                            new_count += 1
                        continue
                    await add_event(
                        "error",
                        f"Collection {collection_id}: sync stopped — not signed in to MakerWorld "
                        f"({new_count} downloaded so far)",
                    )
                    aborted = "auth-required"
                    break
                except CaptchaError as e:
                    # Bambu's anti-abuse layer (HTTP 418) has flagged this
                    # network. Every further request deepens the block, so stop
                    # the whole sync instead of failing model by model.
                    await add_event(
                        "error",
                        f"Collection {collection_id}: sync stopped — Bambu is rate-limiting "
                        f"this network (CAPTCHA challenge); it clears by itself after a while. "
                        f"{new_count} downloaded so far. ({e})",
                    )
                    aborted = "captcha"
                    break
                except MakerWorldError as e:
                    errors.append(f"model {design_id} plate {plate}: {e}")
                    await add_event(
                        "error",
                        f"Collection {collection_id}: model {design_id} failed — {e}",
                    )
            if aborted:
                break

        status = aborted or ("ok" if not errors else f"partial ({len(errors)} errors)")
        self.db.record_sync(collection_id, status, new_count)
        await add_event(
            "sync",
            f"Synced “{title}”: {new_count} new, {len(designs)} total"
            + (f" — stopped early ({aborted})" if aborted else ""),
            collection_id=collection_id,
        )
        return {
            "new": new_count,
            "total": len(designs),
            "errors": errors,
            "status": status,
        }

    async def _design_instances(self, design_id: int) -> list[dict[str, Any]]:
        """Fetch a design's plate instances via a fresh (short-lived) client.

        Used by the 'all plates' sync mode before download_model re-resolves
        everything; isolated here so failures are contained per design.
        """
        client = self._client()
        try:
            env = await client.get_design_instances(design_id)
        finally:
            await release_client(client)
        return env.get("hits") or []


def _find_url(payload: Any) -> str | None:
    """Recursively find the first http(s) URL in a JSON-like payload.

    Bambu's download manifests aren't a stable shape across endpoints, so
    download_url is extracted generically: known keys first ('url',
    'downloadUrl', 'download_url'), then a depth-first value walk.
    """
    if isinstance(payload, str):
        return payload if payload.startswith("http") else None
    if isinstance(payload, dict):
        for key in ("url", "downloadUrl", "download_url"):
            val = payload.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
        for value in payload.values():
            found = _find_url(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_url(item)
            if found:
                return found
    return None


def _safe_filename(name: str) -> str:
    """Sanitize a remote filename into a safe local file name.

    Replaces anything but ASCII letters/digits/._- with underscores, refuses
    hidden-dotfile names, and truncates to 150 chars — no traversal, no
    control characters, no Unicode surprises on the host filesystem.
    Trailing dots/spaces are stripped: Windows and SMB shares reject (or
    silently strip) them, and downloads/ may well be synced to one.
    """
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name or name.startswith("."):
        name = "model_" + name
    name = name.rstrip(". ")  # Windows/SMB-hostile trailing dots and spaces
    if not name or name == "model_":
        name = "model.3mf"
    return name[:150]
