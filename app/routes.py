"""API routes for the downloader."""

from __future__ import annotations

import hmac
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .config import settings
from .db import Database, utcnow
from .downloader import DownloadManager, recent_events
from .makerworld import (
    AuthRequiredError,
    CaptchaError,
    ForbiddenError,
    MakerWorldClient,
    MakerWorldError,
    NotFoundError,
    get_client,
    invalidate_shared_clients,
    parse_collection_url,
    parse_model_url,
    release_client,
)
from .scheduler import SyncScheduler, trigger_sync


async def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """Gate every /api/* route behind the configured shared secret (if any).

    Compares with hmac.compare_digest to avoid timing leaks. When no key is
    configured the app stays open — intended for a trusted LAN.
    """
    if not settings.api_key:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])

# Wiring is injected from main.py at startup (avoids import cycles).
db: Database  # set in init()
manager: DownloadManager
scheduler: SyncScheduler


def init(database: Database, dl_manager: DownloadManager, sched: SyncScheduler) -> None:
    """Wire the module-level singletons from main.py's lifespan.

    Routes import the module, not instances, to avoid import cycles; this is
    called once at startup before any request is served.
    """
    global db, manager, scheduler
    db = database
    manager = dl_manager
    scheduler = sched


class LoginRequest(BaseModel):
    """Body for POST /api/auth/login: credentials + account region."""

    email: str
    password: str
    region: str = "global"


class VerifyRequest(BaseModel):
    """Body for POST /api/auth/verify: the 2FA code completing a login.

    tfa_key selects the TOTP flow (from the login response); without it the
    emailed code flow is used.
    """

    email: str = ""
    code: str
    tfa_key: str = ""
    region: str = "global"


class TokenRequest(BaseModel):
    """Body for POST /api/auth/token: paste an existing Bambu access token."""

    access_token: str
    region: str = "global"


class DownloadRequest(BaseModel):
    """Body for POST /api/download and /api/resolve: a MakerWorld model URL."""

    url: str


class CollectionAddRequest(BaseModel):
    """Body for POST /api/collections: a collection URL + sync interval."""

    url: str
    sync_interval_minutes: int = 360


class CollectionUpdateRequest(BaseModel):
    """Body for PATCH /api/collections/{id}: interval and/or enabled flag."""

    sync_interval_minutes: int | None = None
    enabled: bool | None = None


# ------------------------------------------------------------------ status
# Cache "is the token still valid" answers: the UI polls /status every 30s,
# and Bambu is the authority — but a 401-because-outage must not sign users
# out, and we shouldn't round-trip to Bambu on every poll either.
_TOKEN_CHECK_TTL = 300.0
_token_cache: dict[str, tuple[float, bool]] = {}


def _remember_token_state(token: str, valid: bool) -> None:
    """Cache a token validation result for _TOKEN_CHECK_TTL seconds.

    The cache holds a single entry (single-user app): a new login clears any
    previous token's state so its status is re-checked fresh.
    """
    _token_cache.clear()  # single-user app: only the current token matters
    _token_cache[token] = (time.monotonic() + _TOKEN_CHECK_TTL, valid)


async def _token_state() -> bool | None:
    """True/False cached; None = unknown (never treat as signed-out)."""
    token = db.get_meta("bambu_token")
    if not token:
        return None
    cached = _token_cache.get(token)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    client = get_client()  # anonymous — token is passed per-request
    try:
        valid = await client.validate_token(token)
    finally:
        await release_client(client)
    if valid is not None:
        _remember_token_state(token, valid)
    return valid


@router.get("/status")
async def status() -> dict[str, Any]:
    """App overview for the header badge and Settings tab.

    Reports sign-in state (with a cached live token check — tri-state, so a
    Bambu outage never shows as signed-out), library/collection counts and
    scheduler health.
    """
    token = db.get_meta("bambu_token")
    token_email = db.get_meta("bambu_email")
    token_valid = await _token_state() if token else None
    return {
        "authenticated": bool(token) and token_valid is not False,
        "email": token_email if token else None,
        "token_invalid": token_valid is False,
        "region": db.get_meta("bambu_region") or "global",
        "download_dir": settings.download_dir,
        "model_count": db.count_models(),
        "collection_count": len(db.list_collections()),
        "scheduler": scheduler.status(),
    }


# -------------------------------------------------------------------- auth
@router.post("/auth/login")
async def login(req: LoginRequest) -> dict[str, Any]:
    """Start a Bambu Cloud login with email + password.

    Returns {"step": "done"} and persists the token when Bambu hands one
    over immediately, else {"step": "email_code"|"totp", "tfa_key"} so the
    UI can collect the second factor (POST /api/auth/verify).
    """
    # Ad-hoc client on purpose: login flows (CSRF cookies, pre-auth state)
    # shouldn't share cookies with the pooled identity clients.
    client = MakerWorldClient(region=req.region)
    try:
        result = await client.login(req.email, req.password)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_client(client)
    if result["step"] == "done":
        db.set_meta("bambu_token", result["access_token"])
        db.set_meta("bambu_token_refresh", result.get("refresh_token") or "")
        db.set_meta("bambu_email", req.email)
        db.set_meta("bambu_region", req.region)
        # Stored credentials changed — pooled clients are now stale.
        await invalidate_shared_clients()
        _remember_token_state(result["access_token"], True)
        return {"step": "done", "email": req.email}
    return result


@router.post("/auth/verify")
async def verify(req: VerifyRequest) -> dict[str, Any]:
    """Complete login with the emailed code or a TOTP code.

    On success the token/refresh token/email/region are persisted and the
    validation cache is primed as valid.
    """
    # Ad-hoc client on purpose: login flows (CSRF cookies, pre-auth state)
    # shouldn't share cookies with the pooled identity clients.
    client = MakerWorldClient(region=req.region)
    try:
        if req.tfa_key:
            result = await client.verify_totp(req.tfa_key, req.code)
        else:
            result = await client.verify_email_code(req.email, req.code)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_client(client)
    if result["step"] != "done" or not result.get("access_token"):
        raise HTTPException(status_code=400, detail="Verification failed")
    db.set_meta("bambu_token", result["access_token"])
    db.set_meta("bambu_token_refresh", result.get("refresh_token") or "")
    db.set_meta("bambu_email", req.email)
    db.set_meta("bambu_region", req.region)
    # Stored credentials changed — pooled clients are now stale.
    await invalidate_shared_clients()
    _remember_token_state(result["access_token"], True)
    return {"step": "done", "email": req.email}


@router.post("/auth/token")
async def set_token(req: TokenRequest) -> dict[str, Any]:
    """Sign in by pasting an existing Bambu Cloud access token.

    The token is validated against Bambu first: a rejection is a 400, an
    unreachable Bambu is a 502 (so the UI can say "try again" rather than
    implying the token is bad).
    """
    client = get_client()  # anonymous — the token travels as a parameter
    try:
        valid = await client.validate_token(req.access_token)
    finally:
        await release_client(client)
    if valid is False:
        raise HTTPException(status_code=400, detail="Token rejected by Bambu Cloud")
    if valid is None:
        raise HTTPException(
            status_code=502,
            detail="Could not reach Bambu Cloud to verify the token — try again in a moment",
        )
    db.set_meta("bambu_token", req.access_token)
    db.set_meta("bambu_email", "token-auth")
    db.set_meta("bambu_region", req.region)
    # Stored credentials changed — pooled clients are now stale.
    await invalidate_shared_clients()
    _remember_token_state(req.access_token, True)
    return {"step": "done", "email": "token-auth"}


@router.post("/auth/logout")
async def logout() -> dict[str, Any]:
    """Forget the stored credentials (token, refresh token, email)."""
    for key in ("bambu_token", "bambu_token_refresh", "bambu_email"):
        db.delete_meta(key)
    # Pooled clients carried the old token — drop them so nothing reused
    # after logout still sends it.
    await invalidate_shared_clients()
    _token_cache.clear()
    return {"ok": True}


# ---------------------------------------------------------------- downloads
@router.post("/download")
async def download(req: DownloadRequest) -> dict[str, Any]:
    """Download a model by URL (dedup: re-downloads report {"status": "exists"}).

    Typed client errors map 1:1 to HTTP codes so the UI can tailor its
    messages: auth -> 401, not found -> 404, forbidden/private -> 403,
    rate-limited -> 429, anything else -> 400.
    """
    try:
        result = await manager.download_model(req.url)
    except AuthRequiredError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except CaptchaError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.post("/resolve")
async def resolve(req: DownloadRequest) -> dict[str, Any]:
    """Preview a model URL: metadata + plates, no download."""
    try:
        return await manager.resolve_design(req.url)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/models")
async def models(
    collection_id: int | None = None,
    no_collection: bool = False,
    label: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """List downloaded models, optionally filtered by origin.

    Filters: collection_id (a followed collection), no_collection (manual
    downloads / 'no collection'), label (exact snapshot title match).
    """
    return {
        "models": db.list_models(
            collection_id=collection_id,
            no_collection=no_collection,
            label=label,
            limit=limit,
            offset=offset,
        ),
        "total": db.count_models(
            collection_id=collection_id, no_collection=no_collection, label=label
        ),
    }


@router.get("/model-labels")
async def model_labels() -> list[dict[str, Any]]:
    """Origin labels + counts for the Library filter bar."""
    return db.model_labels()


@router.get("/events")
async def events(limit: int = 50) -> dict[str, Any]:
    """Recent activity-log events (in-memory ring buffer, newest first)."""
    return {"events": recent_events(limit)}


# -------------------------------------------------------------- collections
@router.get("/collections")
async def collections() -> list[dict[str, Any]]:
    """List all followed collections with their sync state."""
    return db.list_collections()


@router.get("/my-collections")
async def my_collections() -> dict[str, Any]:
    """Your own MakerWorld collections with per-collection download checkmarks.

    Serves the hourly-refreshed cache (remote_collections table) — never hits
    MakerWorld, so it stays cheap even if the UI polls it. The listing is
    empty until signed in and either the scheduler's first hourly tick or a
    manual POST /my-collections/refresh has run. Each item carries
    downloaded_count / downloaded / checked_ids for the ✓ UI, plus `followed`
    so the list can show which ones are already being synced.
    """
    rows = db.remote_collections()
    followed = {c["collection_id"]: c for c in db.list_collections()}
    for row in rows:
        row["followed"] = row["collection_id"] in followed
        row["sync_interval_minutes"] = (
            followed[row["collection_id"]]["sync_interval_minutes"] if row["followed"] else None
        )
    return {
        "collections": rows,
        "fetched_at": db.remote_collections_fetched_at(),
        "authenticated": bool(db.get_meta("bambu_token")),
    }


@router.post("/my-collections/refresh")
async def refresh_my_collections_now() -> dict[str, Any]:
    """Re-fetch the own-collections listing from MakerWorld right now.

    Normally the scheduler refreshes it hourly; this exists for the "Refresh"
    button. 401 when signed out, 429 on a CAPTCHA challenge (the listing
    endpoint is subject to the same anti-abuse layer as everything else).
    """
    if not db.get_meta("bambu_token"):
        raise HTTPException(status_code=401, detail="Sign in to MakerWorld first")
    try:
        result = await manager.refresh_my_collections()
    except AuthRequiredError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except CaptchaError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except MakerWorldError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result


@router.post("/collections")
async def add_collection(req: CollectionAddRequest) -> dict[str, Any]:
    """Follow a collection: validate the URL against MakerWorld and register it.

    The stored token is attached so the user's OWN private collections
    resolve; a 403 from MakerWorld is translated into a hint about private
    collections needing the owner's account.
    """
    try:
        collection_id = parse_collection_url(req.url)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Fetch metadata to validate the collection and get its title. Attach the
    # stored token so the user's OWN PRIVATE collections resolve — anonymous
    # requests get 403 on private collections.
    token = db.get_meta("bambu_token")
    region = db.get_meta("bambu_region") or "global"
    client = get_client(auth_token=token, region=region)
    try:
        info = await client.get_collection_info(collection_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Collection not found on MakerWorld")
    except ForbiddenError:
        raise HTTPException(
            status_code=403,
            detail="No access rights to this collection. Sign in first — private "
            "collections need the owner's (or a collaborator's) account.",
        )
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_client(client)
    title = str(info.get("title") or f"collection-{collection_id}")
    db.upsert_collection(collection_id, title, req.url, req.sync_interval_minutes)
    return db.get_collection(collection_id)


@router.patch("/collections/{collection_id}")
async def update_collection(collection_id: int, req: CollectionUpdateRequest) -> dict[str, Any]:
    """Update a followed collection's sync interval and/or paused state."""
    if not db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Collection not registered")
    if req.sync_interval_minutes is not None:
        db.set_collection_interval(collection_id, req.sync_interval_minutes)
    if req.enabled is not None:
        db.set_collection_enabled(collection_id, req.enabled)
    return db.get_collection(collection_id)


@router.delete("/collections/{collection_id}")
async def delete_collection(collection_id: int, delete_files: bool = False) -> dict[str, Any]:
    """Unfollow a collection. With delete_files=true, also remove its
    downloaded model files (and their cover.webp + now-empty folders) and
    the matching library rows.

    Deletion is path-guarded: only files that resolve inside the configured
    downloads directory are ever unlinked, so a corrupted file_path in the
    DB can't make us delete arbitrary host files.
    """
    if not db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Collection not registered")
    deleted_files = 0
    failed = 0
    if delete_files:
        rows = db.list_models(collection_id=collection_id, limit=100000)
        dl_root = Path(settings.download_dir).resolve()
        model_dirs: set[Path] = set()
        for row in rows:
            fp = Path(row["file_path"]).resolve()
            try:
                if fp.is_relative_to(dl_root) and fp.is_file():
                    fp.unlink()
                    deleted_files += 1
                cover = fp.parent / "cover.webp"
                if cover.is_file():
                    cover.unlink()
            except OSError:
                failed += 1
            model_dirs.add(fp.parent)
        # Sweep now-empty model folders and the collection folder itself;
        # rmdir only succeeds when empty, so other content is untouched.
        for d in model_dirs | {p.parent for p in model_dirs}:
            if d == dl_root or not d.is_relative_to(dl_root):
                continue
            try:
                d.rmdir()
            except OSError:
                pass
        db.delete_models(collection_id)
    db.delete_collection(collection_id)
    return {"ok": True, "deleted_files": deleted_files, "failed": failed}


@router.post("/collections/{collection_id}/sync")
async def sync_collection_now(collection_id: int) -> dict[str, Any]:
    """Trigger a background sync of one collection right now.

    Returns {"started": true} or false when a sync for it is already in
    flight. Requires a stored token (401 otherwise) since every download
    needs auth.
    """
    if not db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Collection not registered")
    token = db.get_meta("bambu_token")
    if not token:
        raise HTTPException(status_code=401, detail="Sign in to MakerWorld first")
    started = trigger_sync(manager, collection_id)
    return {"started": started}


# --------------------------------------------------------------- share/quick
@router.get("/shared-model")
async def shared_model(url: str) -> dict[str, Any]:
    """PWA share-target entry: validate a shared MakerWorld URL."""
    try:
        parse_model_url(url)
        return {"valid": True, "url": url}
    except MakerWorldError as e:
        return {"valid": False, "detail": str(e)}