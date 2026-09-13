"""Download manager: resolves models to 3MF URLs and saves them without duplicates."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from .config import settings
from .db import Database
from .makerworld import (
    AuthRequiredError,
    CaptchaError,
    MakerWorldClient,
    MakerWorldError,
    parse_model_url,
)

logger = logging.getLogger(__name__)

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


async def _save_cover_file(client: MakerWorldClient, cover_url: str, model_dir: Path) -> None:
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

    async def backfill_metadata(self) -> None:
        """Fetch covers + creator for models downloaded before those columns
        existed (label titles are migrated in Database.__init__).

        Anonymous design lookups, one at a time with a small delay — same
        politeness rules as syncs so the anti-abuse layer stays calm. Absent
        values are stored as "" (checked) so they aren't re-fetched every boot.
        """
        rows = self.db.models_missing_meta()
        if not rows:
            return
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
            await client.close()
        logger.info("Metadata backfill done")

    def _client(self) -> MakerWorldClient:
        """Build an API client carrying the stored token + region (or None)."""
        token = self.db.get_meta("bambu_token")
        region = self.db.get_meta("bambu_region") or "global"
        return MakerWorldClient(auth_token=token, region=region)

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
            await client.close()
        self.db.replace_remote_collections(mine)
        await add_event(
            "sync",
            f"Refreshed your MakerWorld collections ({len(mine)} found)",
        )
        return {"collections": len(mine), "fetched_at": self.db.remote_collections_fetched_at()}

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
            await client.close()
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
        if self.db.model_exists_any(design_id) if profile_id is None else self.db.model_exists(design_id, profile_id):
            return {"status": "exists", "design_id": design_id, "profile_id": profile_id}

        async with self._sem:
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
                inst_profile_id = int(instance.get("profileId") or 0) if instance else 0
                if self.db.get_meta("bambu_token") and inst_profile_id and alphanumeric_model_id:
                    try:
                        manifest = await client.get_profile_download(
                            inst_profile_id, str(alphanumeric_model_id)
                        )
                        download_url = _find_url(manifest)
                        if isinstance(manifest.get("name"), str) and manifest["name"]:
                            filename_hint = manifest["name"]
                    except (AuthRequiredError, AuthExpiredError):
                        raise
                    except CaptchaError:
                        raise
                    except (NotFoundError, MakerWorldError) as e:
                        logger.warning(
                            "profile download failed for %s (profile %s): %s",
                            design_id, inst_profile_id, e,
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
                        logger.warning("instance download failed for %s: %s", design_id, e)
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
                    raise MakerWorldError("MakerWorld did not return a download URL for this model.")

                # Build destination: downloads/<collection>/<model>/
                parts: list[str] = []
                if subfolder:
                    parts.append(_slugify(subfolder))
                parts.append(_slugify(f"{design_id}-{title}"))
                dest_dir = Path(settings.download_dir).joinpath(*parts)
                dest_dir.mkdir(parents=True, exist_ok=True)

                # Unique temp name so concurrent downloads can't collide.
                tmp = dest_dir / f".{design_id}-{time.monotonic_ns()}.part"
                try:
                    size, remote_name = await client.download_file(download_url, tmp)
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
                    int(instance.get("profileId") or 0) or int(instance.get("id") or 0)
                    if instance else None
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
                origin = f" from “{coll_title}”" if coll_title else " (manual download)"
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
                await client.close()

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
            await client.close()

        self.db.upsert_collection(collection_id, title, coll["url"], coll["sync_interval_minutes"])

        new_count = 0
        errors: list[str] = []
        attempted = 0
        aborted: str | None = None
        for hit in designs:
            design_id = int(hit.get("id") or 0)
            if not design_id:
                continue
            if self.db.model_exists_any(design_id):
                continue
            # Politeness: keep a gap between downloads (and retries) so
            # MakerWorld's anti-abuse layer (HTTP 418) doesn't flag the
            # burst. Skipped-on-first-attempt so single downloads are instant.
            attempted += 1
            if attempted > 1 and settings.download_delay_seconds > 0:
                await asyncio.sleep(settings.download_delay_seconds)
            try:
                result = await self.download_model(
                    f"https://makerworld.com/en/models/{design_id}",
                    collection_id=collection_id,
                    subfolder=title,
                )
                if result["status"] == "downloaded":
                    new_count += 1
            except AuthRequiredError:
                # No point hammering the remaining models — the whole sync
                # is dead until the user signs in. Stop here; progress so
                # far is kept and recorded below.
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
                errors.append(f"model {design_id}: {e}")
                await add_event("error", f"Collection {collection_id}: model {design_id} failed — {e}")

        status = aborted or ("ok" if not errors else f"partial ({len(errors)} errors)")
        self.db.record_sync(collection_id, status, new_count)
        await add_event(
            "sync",
            f"Synced “{title}”: {new_count} new, {len(designs)} total"
            + (f" — stopped early ({aborted})" if aborted else ""),
            collection_id=collection_id,
        )
        return {"new": new_count, "total": len(designs), "errors": errors, "status": status}


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
    """
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name or name.startswith("."):
        name = "model_" + name
    return name[:150]