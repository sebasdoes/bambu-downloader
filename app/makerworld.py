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
- Collection meta: GET  {mw}/api/v1/design-service/favorites/{cid}/withoutdesign
- Collection items: GET  {mw}/api/v1/design-service/favorites/{cid}/designs?limit=&offset=

makerworld.com's /api/* JSON gateway is not Cloudflare-challenged for plain
HTTP clients; the HTML pages are. api.bambulab.com is fully open.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import settings

# Bambu's 401 signature for an expired session.
EXPIRED_401_CODES = {4}


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
            return await self._download_s3_verbatim(url, dest_path)
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

    async def _download_s3_verbatim(self, url: str, dest_path: Path) -> tuple[int, str]:
        """Fetch an S3 presigned URL with urllib, which transmits the URL
        verbatim (httpx would re-encode the query and break SigV4)."""
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