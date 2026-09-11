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
    parse_collection_url,
    parse_model_url,
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
    global db, manager, scheduler
    db = database
    manager = dl_manager
    scheduler = sched


class LoginRequest(BaseModel):
    email: str
    password: str
    region: str = "global"


class VerifyRequest(BaseModel):
    email: str = ""
    code: str
    tfa_key: str = ""
    region: str = "global"


class TokenRequest(BaseModel):
    access_token: str
    region: str = "global"


class DownloadRequest(BaseModel):
    url: str


class CollectionAddRequest(BaseModel):
    url: str
    sync_interval_minutes: int = 360


class CollectionUpdateRequest(BaseModel):
    sync_interval_minutes: int | None = None
    enabled: bool | None = None


# ------------------------------------------------------------------ status
# Cache "is the token still valid" answers: the UI polls /status every 30s,
# and Bambu is the authority — but a 401-because-outage must not sign users
# out, and we shouldn't round-trip to Bambu on every poll either.
_TOKEN_CHECK_TTL = 300.0
_token_cache: dict[str, tuple[float, bool]] = {}


def _remember_token_state(token: str, valid: bool) -> None:
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
    client = MakerWorldClient()
    try:
        valid = await client.validate_token(token)
    finally:
        await client.close()
    if valid is not None:
        _remember_token_state(token, valid)
    return valid


@router.get("/status")
async def status() -> dict[str, Any]:
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
    client = MakerWorldClient(region=req.region)
    try:
        result = await client.login(req.email, req.password)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()
    if result["step"] == "done":
        db.set_meta("bambu_token", result["access_token"])
        db.set_meta("bambu_token_refresh", result.get("refresh_token") or "")
        db.set_meta("bambu_email", req.email)
        db.set_meta("bambu_region", req.region)
        _remember_token_state(result["access_token"], True)
        return {"step": "done", "email": req.email}
    return result


@router.post("/auth/verify")
async def verify(req: VerifyRequest) -> dict[str, Any]:
    client = MakerWorldClient(region=req.region)
    try:
        if req.tfa_key:
            result = await client.verify_totp(req.tfa_key, req.code)
        else:
            result = await client.verify_email_code(req.email, req.code)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()
    if result["step"] != "done" or not result.get("access_token"):
        raise HTTPException(status_code=400, detail="Verification failed")
    db.set_meta("bambu_token", result["access_token"])
    db.set_meta("bambu_token_refresh", result.get("refresh_token") or "")
    db.set_meta("bambu_email", req.email)
    db.set_meta("bambu_region", req.region)
    _remember_token_state(result["access_token"], True)
    return {"step": "done", "email": req.email}


@router.post("/auth/token")
async def set_token(req: TokenRequest) -> dict[str, Any]:
    client = MakerWorldClient()
    try:
        valid = await client.validate_token(req.access_token)
    finally:
        await client.close()
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
    _remember_token_state(req.access_token, True)
    return {"step": "done", "email": "token-auth"}


@router.post("/auth/logout")
async def logout() -> dict[str, Any]:
    for key in ("bambu_token", "bambu_token_refresh", "bambu_email"):
        db.delete_meta(key)
    _token_cache.clear()
    return {"ok": True}


# ---------------------------------------------------------------- downloads
@router.post("/download")
async def download(req: DownloadRequest) -> dict[str, Any]:
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
    return {"events": recent_events(limit)}


# -------------------------------------------------------------- collections
@router.get("/collections")
async def collections() -> list[dict[str, Any]]:
    return db.list_collections()


@router.post("/collections")
async def add_collection(req: CollectionAddRequest) -> dict[str, Any]:
    try:
        collection_id = parse_collection_url(req.url)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Fetch metadata to validate the collection and get its title. Attach the
    # stored token so the user's OWN PRIVATE collections resolve — anonymous
    # requests get 403 on private collections.
    token = db.get_meta("bambu_token")
    region = db.get_meta("bambu_region") or "global"
    client = MakerWorldClient(auth_token=token, region=region)
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
        await client.close()
    title = str(info.get("title") or f"collection-{collection_id}")
    db.upsert_collection(collection_id, title, req.url, req.sync_interval_minutes)
    return db.get_collection(collection_id)


@router.patch("/collections/{collection_id}")
async def update_collection(collection_id: int, req: CollectionUpdateRequest) -> dict[str, Any]:
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