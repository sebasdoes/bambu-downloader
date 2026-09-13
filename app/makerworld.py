"""MakerWorld / Bambu Cloud API client.

Endpoints verified against live traffic on 2026-09-11:

- Login:            POST {bambu_api}/v1/user-service/user/login
                     {"account": email, "password": pw}  ->
                       200 {"loginType": "verifyCode"}                 (email code flow)
                       200 {"loginType": "tfa", "tfaKey": ...}         (TOTP flow)
                       200 {"accessToken": ..., "refreshToken": ...}   (direct)
                     Second step (email): POST same endpoint with {"account", "code"}
                     Second step (TOTP):  POST {web_origin}/api/sign-in/tfa with
                       double-submit CSRF (GET {web_origin}/api/csrf mints the
                       bbl_csrf_token cookie; echo it in x-bbl-csrf-token header)
- Token validation: GET  {bambu_api}/v1/design-user-service/my/preference
- Design metadata: GET  {mw}/api/v1/design-service/design/{designId}          (anon ok)
- Plate instances:  GET  {mw}/api/v1/design-service/design/{designId}/instances (anon ok)
- Model download:  GET  {mw}/api/v1/design-service/design/{designId}/model     (auth)
- Plate 3MF:       GET  {mw}/api/v1/design-service/instance/{instanceId}/f3mf  (auth)
- Profile DL:      GET  {bambu_api}/v1/iot-service/api/user/profile/{profileId}?model_id=  (auth)
- Collections tab: GET  {mw}/api/v1/design-service/favorites-collections/tab
                     (auth; {"total", "hits": [...]} — each hit is one of the
                     signed-in account's own collections with id/title/slug/
                     designCnt/isDefault plus an embedded first page of
                     designs; supports ?limit=&offset= pagination)
- Collection meta: GET  {mw}/api/v1/design-service/favorites/{cid}/withoutdesign
- Collection items: GET  {mw}/api/v1/design-service/favorites/{cid}/designs?limit=&offset=

makerworld.com's /api/* JSON gateway is not Cloudflare-challenged for plain
HTTP clients; the HTML pages are. api.bambulab.com is fully open.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# Bambu's 401 signature for an expired session.
EXPIRED_401_CODES = {4}

# Design-id top-up paging (list_my_collections completing big collections):
# page size per request and a hard page-count guard against endless loops.
CAP_PAGE_SIZE = 100
CAP_MAX_PAGES = 10


class MakerWorldError(Exception):
    """Base error talking to MakerWorld."""


class AuthRequiredError(MakerWorldError):
    """Call needs a signed-in Bambu Cloud account."""


class AuthExpiredError(AuthRequiredError):
    """Stored token was rejected — user must sign in again. Subclasses
    AuthRequiredError so every 'not signed in' handler also covers expiry."""


class ForbiddenError(MakerWorldError):
    """MakerWorld refuses the resource (purchase/points/region gated)."""


class NotFoundError(MakerWorldError):
    """Design or collection does not exist."""


class CaptchaError(MakerWorldError):
    """Bambu is challenging this network with a CAPTCHA (418)."""

    def __init__(self, message: str = "") -> None:
        """Build the error, defaulting to the actionable rate-limit explanation."""
        super().__init__(
            message
            or "MakerWorld's anti-abuse layer is rate-limiting this network (HTTP 418). "
            "It is tied to your IP and normally clears within a few hours — "
            "repeated requests make it last longer. Try again later."
        )


_MODEL_URL_RE = re.compile(r"/models/(\d+)")
_COLLECTION_URL_RE = re.compile(r"/collections/(\d+)")
_PROFILE_HASH_RE = re.compile(r"#profileId-(\d+)")


def parse_model_url(url: str) -> tuple[int, int | None]:
    """Extract (design_id, profile_id) from any MakerWorld model URL."""
    m = _MODEL_URL_RE.search(url)
    if not m:
        raise MakerWorldError("Not a MakerWorld model URL (expected .../models/<id>-slug)")
    design_id = int(m.group(1))
    profile_id = None
    pm = _PROFILE_HASH_RE.search(url)
    if pm:
        profile_id = int(pm.group(1))
    return design_id, profile_id


def parse_collection_url(url: str) -> int:
    """Extract collection_id from a MakerWorld collection URL."""
    m = _COLLECTION_URL_RE.search(url)
    if not m:
        raise MakerWorldError("Not a MakerWorld collection URL (expected .../collections/<id>-slug)")
    return int(m.group(1))


def _detect_captcha(status_code: int, body: dict[str, Any] | str) -> bool:
    """Heuristically detect Bambu's anti-abuse CAPTCHA challenge (HTTP 418).

    The challenge also leaks into non-418 responses as 'captcha'/'robot'
    text in the error or message fields, so both are checked.
    """
    if status_code == 418:
        return True
    if isinstance(body, str) and "captcha" in body.lower():
        return True
    if isinstance(body, dict):
        text = f"{body.get('error', '')} {body.get('message', '')}".lower()
        if "captcha" in text or "robot" in text:
            return True
    return False


class MakerWorldClient:
    """Async client for MakerWorld/Bambu Cloud."""

    def __init__(self, auth_token: str | None = None, region: str = "global") -> None:
        """Create a client; auth_token (when given) is attached to authed calls.

        region selects the Bambu Cloud base ('global' -> api.bambulab.com,
        'china' -> api.bambulab.cn). The underlying httpx client is honest
        about its identity (see settings.user_agent) — no browser spoofing.
        """
        self.auth_token = auth_token
        self.region = region
        base = settings.bambu_api_base if region != "china" else settings.bambu_api_base_cn
        self.bambu_api = base.rstrip("/")
        self.mw = settings.makerworld_base.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=120.0),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://makerworld.com/",
            },
            follow_redirects=True,
        )

    async def close(self) -> None:
        """Release the underlying httpx connection pool (always call this)."""
        await self._client.aclose()

    # ------------------------------------------------------------- auth flow
    async def _fetch_csrf_token(self, web_origin: str) -> str | None:
        """Mint a bbl_csrf_token cookie via GET {origin}/api/csrf (204)."""
        try:
            await self._client.get(f"{web_origin}/api/csrf")
        except httpx.HTTPError:
            return None
        return self._client.cookies.get("bbl_csrf_token")

    async def login(self, email: str, password: str) -> dict[str, Any]:
        """Start login. Returns dict describing the next step."""
        try:
            resp = await self._client.post(
                f"{self.bambu_api}/v1/user-service/user/login",
                json={"account": email, "password": password},
            )
        except httpx.HTTPError as e:
            raise MakerWorldError(f"Could not reach Bambu Cloud: {e}") from e

        if _detect_captcha(resp.status_code, _safe_json(resp)):
            raise CaptchaError(
                "Bambu is challenging this network with a CAPTCHA. Wait a few hours "
                "or paste an access token from a browser session instead."
            )

        data = _safe_json(resp)
        if resp.status_code != 200 or not isinstance(data, dict):
            msg = data.get("error") or data.get("message") or "Login failed" if isinstance(data, dict) else "Login failed"
            raise MakerWorldError(str(msg))

        login_type = data.get("loginType")
        # Check for a REAL token: the key can be present but empty/null when
        # a verification code or TOTP is still required. Checking key presence
        # alone made the UI skip the code step and pretend login succeeded.
        token = data.get("accessToken")
        if isinstance(token, str) and token.strip():
            return {
                "step": "done",
                "access_token": token,
                "refresh_token": data.get("refreshToken"),
            }
        if login_type == "verifyCode":
            return {"step": "email_code"}
        if login_type == "tfa" or "tfaKey" in data:
            return {"step": "totp", "tfa_key": data.get("tfaKey")}
        raise MakerWorldError(f"Unexpected login response: {data}")

    async def verify_email_code(self, email: str, code: str) -> dict[str, Any]:
        """Complete login with the emailed 6-digit code."""
        try:
            resp = await self._client.post(
                f"{self.bambu_api}/v1/user-service/user/login",
                json={"account": email, "code": code},
            )
        except httpx.HTTPError as e:
            raise MakerWorldError(f"Could not reach Bambu Cloud: {e}") from e
        data = _safe_json(resp)
        if resp.status_code == 200 and isinstance(data, dict) and data.get("accessToken"):
            return {
                "step": "done",
                "access_token": data["accessToken"],
                "refresh_token": data.get("refreshToken"),
            }
        msg = data.get("message") or data.get("error") or "Verification failed" if isinstance(data, dict) else "Verification failed"
        raise MakerWorldError(str(msg))

    async def verify_totp(self, tfa_key: str, code: str) -> dict[str, Any]:
        """Complete login with a TOTP code. Uses the web origin + CSRF double submit."""
        web_origin = "https://bambulab.cn" if self.region == "china" else "https://bambulab.com"
        csrf = await self._fetch_csrf_token(web_origin)
        if not csrf:
            raise MakerWorldError("Could not obtain a CSRF token from Bambu Cloud.")
        try:
            resp = await self._client.post(
                f"{web_origin}/api/sign-in/tfa",
                headers={
                    "Content-Type": "application/json",
                    "x-bbl-csrf-token": csrf,
                },
                json={"tfaKey": tfa_key, "tfaCode": code},
            )
        except httpx.HTTPError as e:
            raise MakerWorldError(f"Could not reach Bambu Cloud: {e}") from e
        if _detect_captcha(resp.status_code, _safe_json(resp)):
            raise CaptchaError("Bambu is challenging this network with a CAPTCHA.")
        data = _safe_json(resp)
        token = None
        if isinstance(data, dict):
            token = data.get("accessToken") or data.get("token")
        if resp.status_code == 200 and token:
            # Token may also arrive via cookies.
            for name in ("token", "accessToken"):
                if self._client.cookies.get(name):
                    token = self._client.cookies.get(name)
                    break
            return {"step": "done", "access_token": token, "refresh_token": data.get("refreshToken")}
        msg = data.get("message") or data.get("error") or "Invalid code" if isinstance(data, dict) else "Invalid code"
        raise MakerWorldError(str(msg))

    async def validate_token(self, token: str) -> bool | None:
        """Ask Bambu whether the token is still accepted.

        True  — accepted
        False — Bambu rejected it (any 401 from the preference endpoint; the
                body-based signature check was dropped because Bambu now
                returns an empty 401 body, which made expiry undetectable)
        None  — unknown (network error / 5xx / other ambiguous status);
                callers must NOT treat None as sign-out, or a Bambu outage
                would sign the user out of a perfectly good session.
        """
        try:
            resp = await self._client.get(
                f"{self.bambu_api}/v1/design-user-service/my/preference",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            return None
        if resp.status_code == 200:
            return True
        if resp.status_code == 401:
            return False
        return None

    # ----------------------------------------------------------- mw gateway
    async def _mw_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        """GET a makerworld.com /api/* JSON gateway path with error mapping.

        auth=True attaches the stored Bearer token and raises AuthRequiredError
        when absent. Responses are translated to typed exceptions: CAPTCHA (418
        or textual hint), 401 -> expired, 403 with "please log in" -> auth
        required, other 403s -> ForbiddenError (with a private-collection
        hint when a token was attached), 404 -> NotFoundError. The gateway
        sometimes labels JSON as text/plain, so the body is parsed leniently.
        """
        headers: dict[str, str] = {}
        if auth:
            if not self.auth_token:
                raise AuthRequiredError("Sign in to MakerWorld first (Settings → Login).")
            headers["Authorization"] = f"Bearer {self.auth_token}"
        try:
            resp = await self._client.get(
                f"{self.mw}{path}",
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as e:
            raise MakerWorldError(f"Could not reach MakerWorld: {e}") from e

        if _detect_captcha(resp.status_code, _safe_json(resp)):
            raise CaptchaError("MakerWorld is challenging this network with a CAPTCHA.")

        if resp.status_code == 401:
            raise AuthExpiredError("Your MakerWorld sign-in has expired. Sign in again.")
        if resp.status_code == 403:
            data = _safe_json(resp)
            if isinstance(data, dict) and "please log in" in str(data.get("error", "")).lower():
                raise AuthRequiredError("Sign in to MakerWorld first (Settings → Login).")
            msg = data.get("error") or "Access denied" if isinstance(data, dict) else "Access denied"
            if self.auth_token:
                # We DID send a token and were still refused — a truly private
                # resource this account can't see.
                raise ForbiddenError(
                    f"No access rights to this MakerWorld resource ({msg}). "
                    "Private collections need the owner's account."
                )
            raise ForbiddenError(f"MakerWorld refused access ({msg}) — try signing in.")
        if resp.status_code == 404:
            raise NotFoundError("MakerWorld resource not found (check the URL).")
        if resp.status_code != 200:
            raise MakerWorldError(f"MakerWorld returned HTTP {resp.status_code} for {path}")

        # The gateway sometimes returns JSON with content-type text/plain.
        try:
            return resp.json()
        except Exception as e:
            raise MakerWorldError(f"Unexpected response from MakerWorld for {path}: {resp.text[:200]}") from e

    # ------------------------------------------------------------ public API
    async def get_design(self, design_id: int) -> dict[str, Any]:
        """Design metadata. Attach the token when present: public designs
        ignore it (verified) and private ones REQUIRE it."""
        return await self._mw_get(
            f"/api/v1/design-service/design/{design_id}",
            auth=bool(self.auth_token),
        )

    async def get_design_instances(self, design_id: int) -> dict[str, Any]:
        """List a design's plate instances ({"total", "hits": [...]}).

        The token is attached when present for the same public/private
        reason as get_design.
        """
        data = await self._mw_get(
            f"/api/v1/design-service/design/{design_id}/instances",
            auth=bool(self.auth_token),
        )
        if isinstance(data, dict):
            return data
        raise MakerWorldError("Unexpected instances payload")

    async def get_design_model_download(self, design_id: int) -> dict[str, Any]:
        """Design-level download URL. Legacy path — newer models answer 400."""
        return await self._mw_get(f"/api/v1/design-service/design/{design_id}/model", auth=True)

    async def get_instance_download(self, instance_id: int) -> dict[str, Any]:
        """Per-plate 3MF URL. Legacy path — newer models answer 400."""
        return await self._mw_get(f"/api/v1/design-service/instance/{instance_id}/f3mf", auth=True)

    async def get_profile_download(self, profile_id: int, model_id: str) -> dict[str, Any]:
        """Fetch the signed 3MF download manifest via Bambu's iot-service.

        GET {bambu_api}/v1/iot-service/api/user/profile/{profileId}?model_id={modelId}
        with the stored Bearer token. ``model_id`` is the design's ALPHANUMERIC
        id (e.g. "US2bb73b106683e5" — the ``modelId`` field of the design
        payload), NOT the numeric design id.

        This is the reliable download path (per Bambuddy / YASTL#51): the
        makerworld.com design/instance download endpoints are dead for newer
        models (HTTP 400) and are cookie-gated in browsers. This endpoint
        lives on api.bambulab.com (not Cloudflare-challenged) and mints the
        signed CDN URL from the same long-lived bearer the user logs in with.

        Returns {"url": "...", "name": "..."} — the URL is short-lived.
        """
        if not self.auth_token:
            raise AuthRequiredError("Sign in to MakerWorld first (Settings → Login).")
        try:
            resp = await self._client.get(
                f"{self.bambu_api}/v1/iot-service/api/user/profile/{profile_id}",
                params={"model_id": str(model_id)},
                headers={"Authorization": f"Bearer {self.auth_token}"},
            )
        except httpx.HTTPError as e:
            raise MakerWorldError(f"Could not reach Bambu Cloud: {e}") from e
        if _detect_captcha(resp.status_code, _safe_json(resp)):
            raise CaptchaError()
        if resp.status_code == 401:
            raise AuthExpiredError("Your Bambu sign-in has expired. Sign in again.")
        if resp.status_code == 403:
            raise AuthRequiredError("Sign in to MakerWorld first (Settings → Login).")
        if resp.status_code == 404:
            raise NotFoundError("Profile not found on Bambu Cloud.")
        if resp.status_code != 200:
            raise MakerWorldError(f"Bambu Cloud returned HTTP {resp.status_code} for the download manifest")
        data = _safe_json(resp)
        if isinstance(data, dict):
            return data
        raise MakerWorldError("Unexpected download manifest payload")

    async def get_collection_info(self, collection_id: int) -> dict[str, Any]:
        """Collection metadata. Sends the Bearer token when we have one so the
        user's OWN private collections resolve — anonymous requests get 403
        "no access rights" on private collections. The endpoint ignores
        invalid tokens for public collections (verified), so always attaching
        the token is safe and widens what we can list."""
        return await self._mw_get(
            f"/api/v1/design-service/favorites/{collection_id}/withoutdesign",
            auth=bool(self.auth_token),
        )

    async def get_collection_designs_page(self, collection_id: int, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Collection design listing — same optional-auth treatment as above."""
        return await self._mw_get(
            f"/api/v1/design-service/favorites/{collection_id}/designs",
            params={"limit": limit, "offset": offset},
            auth=bool(self.auth_token),
        )

    async def get_my_collections_page(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """One page of the signed-in user's own collections (auth required).

        GET /api/v1/design-service/favorites-collections/tab — verified live:
        {"total": int, "hits": [<collection>]} where each collection carries
        id, title, slug, designCnt, isDefault and an embedded `designs` array
        (the first page of its items, enough to resolve ids for small
        collections without per-collection round trips).
        """
        return await self._mw_get(
            "/api/v1/design-service/favorites-collections/tab",
            params={"limit": limit, "offset": offset},
            auth=True,
        )

    async def list_my_collections(
        self,
        page_size: int = 50,
        max_designs_per_collection: int = 1000,
    ) -> list[dict[str, Any]]:
        """All of the signed-in user's own collections, walking pagination.

        Returns a normalized list of
        {collection_id, title, slug, design_count, is_default, design_ids}.

        The tab endpoint embeds only the FIRST page of each collection's
        designs (~100 ids), so collections with designCnt beyond that get
        their id list completed via the favorites/{cid}/designs pager —
        otherwise the UI's "✓ all downloaded" could never trigger for big
        collections (downloaded_count could never reach design_count).
        Capped at max_designs_per_collection per collection so one
        10,000-model collection can't turn into a crawl; a partial list
        still shows correct n/m checkmarks for the ids we do have. A small
        delay between paging requests keeps the anti-abuse layer (418) calm.
        """
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = await self.get_my_collections_page(limit=page_size, offset=offset)
            hits = data.get("hits") or []
            for hit in hits:
                design_ids: list[int] = []
                for d in (hit.get("designs") or []):
                    try:
                        design_ids.append(int(d.get("id") or 0))
                    except (TypeError, ValueError):
                        continue
                design_ids = [i for i in design_ids if i]

                collection_id = int(hit.get("id") or 0)
                design_count = int(hit.get("designCnt") or 0)
                # Top up short lists from the collection's own pager (skip
                # when the embedded page already covers everything, or when
                # the cap can't change the outcome).
                if (
                    collection_id
                    and design_count > len(design_ids)
                    and len(design_ids) < max_designs_per_collection
                ):
                    try:
                        extra = await self._collect_design_ids(
                            collection_id,
                            known=set(design_ids),
                            cap=max_designs_per_collection,
                        )
                        design_ids.extend(extra)
                    except MakerWorldError as e:
                        # Keep the collection with partial ids rather than
                        # failing the whole listing over one paging hiccup.
                        logger.warning(
                            "design list top-up for collection %s failed: %s",
                            collection_id, e,
                        )
                out.append(
                    {
                        "collection_id": collection_id,
                        "title": str(hit.get("title") or ""),
                        "slug": str(hit.get("slug") or ""),
                        "design_count": design_count,
                        "is_default": bool(hit.get("isDefault")),
                        "design_ids": design_ids,
                    }
                )
            offset += len(hits)
            total = int(data.get("total") or 0)
            if not hits or offset >= total:
                break
        return [c for c in out if c["collection_id"]]

    async def _collect_design_ids(
        self,
        collection_id: int,
        known: set[int],
        cap: int,
    ) -> list[int]:
        """Page through favorites/{cid}/designs collecting NEW design ids.

        Used to complete the embedded page-1 id list for larger collections.
        Stops at `cap` ids total, when the server reports fewer total items,
        on an empty page, or at the CAP_MAX_PAGES guard (belt against a
        pathological endless pagination). Politeness delay between pages.
        """
        extra: list[int] = []
        fetched = 0
        for _ in range(CAP_MAX_PAGES):
            if settings.download_delay_seconds > 0:
                await asyncio.sleep(settings.download_delay_seconds)
            page = await self.get_collection_designs_page(
                collection_id, limit=CAP_PAGE_SIZE, offset=fetched
            )
            hits = page.get("hits") or []
            if not hits:
                break
            for d in hits:
                try:
                    did = int(d.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if did and did not in known and len(known) + len(extra) < cap:
                    extra.append(did)
            fetched += len(hits)
            if fetched >= int(page.get("total") or 0) or len(known) + len(extra) >= cap:
                break
        return extra

    async def list_collection_designs(self, collection_id: int, page_size: int = 100) -> list[dict[str, Any]]:
        """Walk pagination and return all designs in a collection."""
        all_hits: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = await self.get_collection_designs_page(collection_id, limit=page_size, offset=offset)
            hits = data.get("hits") or []
            all_hits.extend(hits)
            total = int(data.get("total") or 0)
            offset += len(hits)
            if not hits or offset >= total:
                break
        return all_hits

    # ------------------------------------------------------------- downloads
    async def download_file(self, url: str, dest_path: Path) -> tuple[int, str]:
        """Stream a presigned download URL to dest_path (a temp file).

        Returns (size_bytes, filename). Presigned S3 URLs compute their
        signature over the EXACT query-string bytes; httpx re-serializes
        URLs through urlencode which breaks the signature (HTTP 400
        SignatureDoesNotMatch — verified by Bambuddy/YASTL#52). So S3 hosts
        are fetched verbatim via urllib; MakerWorld's own CDN via httpx.
        The signed URL's query IS the credential — never attach auth headers.
        """
        try:
            parsed = urlparse(url)
        except ValueError as e:
            raise MakerWorldError(f"Invalid download URL: {e}") from e
        host = (parsed.hostname or "").lower()
        if host.endswith(".amazonaws.com"):
            # urllib is synchronous; run it in a thread so a large S3
            # transfer can't stall the event loop (UI, scheduler, thumbs).
            return await asyncio.to_thread(self._download_s3_verbatim, url, dest_path)
        try:
            async with self._client.stream(
                "GET", url, headers={"User-Agent": settings.user_agent}
            ) as resp:
                if resp.status_code != 200:
                    raise MakerWorldError(f"Download failed with HTTP {resp.status_code}")
                size = 0
                with open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        size += len(chunk)
                        f.write(chunk)
                filename = _filename_from_response(resp, url)
                return size, filename
        except httpx.HTTPError as e:
            raise MakerWorldError(f"Download failed: {e}") from e

    def _download_s3_verbatim(self, url: str, dest_path: Path) -> tuple[int, str]:
        """Fetch an S3 presigned URL with urllib, which transmits the URL
        verbatim (httpx would re-encode the query and break SigV4).

        Deliberately SYNCHRONOUS: it runs inside a worker thread via
        asyncio.to_thread (see download_file), so a large transfer blocks
        the thread, never the event loop.
        """
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": settings.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status != 200:
                    raise MakerWorldError(f"Download failed with HTTP {resp.status_code}")
                size = 0
                with open(dest_path, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        f.write(chunk)
                filename = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "model.3mf"
                return size, filename
        except Exception as e:
            raise MakerWorldError(f"Download failed: {e}") from e

    async def fetch_thumbnail(self, url: str, width: int = 512) -> bytes:
        """Fetch a cover image, resized on the fly via the CDN's OSS params
        (a 4MB 1920px PNG becomes a ~22KB 512px WebP). Skips the resize when
        the URL already carries an x-oss-process param."""
        if "x-oss-process" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}x-oss-process=image/resize,w_{width}"
        resp = await self._client.get(url)
        if resp.status_code != 200:
            raise MakerWorldError(f"Thumbnail fetch failed: HTTP {resp.status_code}")
        return resp.content


def _safe_json(resp: httpx.Response) -> Any:
    """Parse a response as JSON; on failure return a truncated text snippet.

    Callers use this to inspect error bodies that may be JSON, HTML, or
    empty — anything parseable beats raising mid error-handling.
    """
    try:
        return resp.json()
    except Exception:
        return resp.text[:500]


# ---------------------------------------------------------------- shared pool
# One httpx connection pool per (token, region) identity: repeated API calls
# reuse warm TLS connections instead of paying a fresh handshake each time.
# Cookies live on the client too, but pooling is per-identity so nothing
# leaks across accounts; the anonymous entry only ever accumulates harmless
# CSRF cookies that the TOTP flow re-mints anyway.
_client_pool: dict[tuple[str | None, str], MakerWorldClient] = {}


def get_client(auth_token: str | None = None, region: str = "global") -> MakerWorldClient:
    """Return a long-lived pooled client for this (token, region) identity.

    Created on first use, reused after. Pooled clients must NOT be closed
    by callers — release them with release_client(), which keeps pooled
    ones warm. When the stored token/region changes, call
    invalidate_shared_clients() so stale identities are closed and dropped.
    """
    key = (auth_token, region)
    client = _client_pool.get(key)
    if client is None:
        client = MakerWorldClient(auth_token=auth_token, region=region)
        _client_pool[key] = client
    return client


async def release_client(client: MakerWorldClient) -> None:
    """Release a client after use: pooled ones stay warm, ad-hoc ones close.

    Drop-in replacement for the old `await client.close()` discipline so
    call sites keep their try/finally shape — the only change is that
    pooled clients survive the release.
    """
    if not any(client is pooled for pooled in _client_pool.values()):
        await client.close()


async def invalidate_shared_clients() -> None:
    """Close and drop ALL pooled clients.

    Must be called whenever the stored token or region changes (login,
    verify, token paste, logout) so the next call with new credentials
    builds a fresh client and the stale identity's connections are freed.
    """
    pooled = list(_client_pool.values())
    _client_pool.clear()
    for client in pooled:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 — teardown must never raise
            logger.warning("error closing pooled MakerWorld client", exc_info=True)


def _filename_from_response(resp: httpx.Response, url: str) -> str:
    """Pick a download filename: Content-Disposition first, URL tail second."""
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return m.group(1)
    path = url.split("?")[0].rstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name or "model"


# ---------------------------------------------------------------------- misc
def new_request_id() -> str:
    """Generate a random hex request id (unused reserved helper)."""
    return uuid.uuid4().hex