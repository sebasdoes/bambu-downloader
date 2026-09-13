"""Unit tests for app/makerworld.py — URL parsing, error mapping, helpers.

Network is never touched: HTTP behavior is tested with a mock transport
(httpx.MockTransport) wired through a real MakerWorldClient, so _mw_get's
error translation and download_host selection are covered end to end.

NOTE on exception identity: other test files reload app.makerworld (their
fixtures need fresh settings), which rebinds the module's exception
classes. This module therefore resolves exceptions through `mw` (the live
module) at call time instead of binding them at import time.
"""

from __future__ import annotations

import httpx
import pytest

import app.makerworld as mw
from app.downloader import _find_url
from app.makerworld import (
    MakerWorldClient,  # class object is stable enough for construction
    _detect_captcha,
    _filename_from_response,
    _safe_json,
    get_client,
    invalidate_shared_clients,
    parse_collection_url,
    parse_model_url,
    release_client,
)


# Resolve exception classes lazily so reloads in other test files don't
# strand stale class objects here.
def _exc(name: str):
    return getattr(mw, name)


# ---------------------------------------------------------------- URL parsing
def test_parse_model_url_plain():
    assert parse_model_url("https://makerworld.com/en/models/1234567-slug") == (
        1234567,
        None,
    )


def test_parse_model_url_with_profile():
    url = "https://makerworld.com/en/models/1234567-slug#profileId-42"
    assert parse_model_url(url) == (1234567, 42)


def test_parse_model_url_rejects_non_model():
    with __import__("pytest").raises(_exc("MakerWorldError")):
        parse_model_url("https://makerworld.com/en/collections/1-x")


def test_parse_collection_url():
    assert parse_collection_url("https://makerworld.com/en/collections/99-slug") == 99


def test_parse_collection_url_rejects_model():
    with __import__("pytest").raises(_exc("MakerWorldError")):
        parse_collection_url("https://makerworld.com/en/models/1-x")


def test_parse_model_url_midpath_fragment():
    # profile fragment can appear anywhere; regex must not match wrong ids
    url = "https://makerworld.com/en/models/555-my#profileId-7?other=1"
    assert parse_model_url(url) == (555, 7)


# ------------------------------------------------------------- error helpers
def test_detect_captcha_418():
    assert _detect_captcha(418, {}) is True


def test_detect_captcha_textual():
    assert _detect_captcha(200, {"error": "captcha required"}) is True
    assert _detect_captcha(200, {"message": "robot check"}) is True
    assert _detect_captcha(200, {"error": "ok"}) is False
    # plain-string bodies only match 'captcha' (dict branch handles 'robot')
    assert _detect_captcha(200, "captcha wall") is True


def test_find_url_known_keys_and_recursion():
    assert _find_url({"url": "https://a"}) == "https://a"
    assert _find_url({"downloadUrl": "https://b"}) == "https://b"
    assert (
        _find_url({"a": {"b": [{"c": {"download_url": "https://d"}}]}}) == "https://d"
    )
    assert _find_url({"url": "not-http"}) is None
    assert _find_url(42) is None


def test_filename_from_response_prefer_content_disposition():
    resp = httpx.Response(
        200, headers={"content-disposition": 'attachment; filename="x.3mf"'}
    )
    assert _filename_from_response(resp, "https://a/b/y.3mf") == "x.3mf"


def test_filename_from_response_url_tail():
    resp = httpx.Response(200)
    assert _filename_from_response(resp, "https://a/b/file.3mf?sig=1") == "file.3mf"


def test_safe_json_lenient():
    assert _safe_json(httpx.Response(200, json={"a": 1})) == {"a": 1}
    assert _safe_json(httpx.Response(200, text="<html>")) == "<html>"


# ------------------------------------------------------------ _mw_get errors
def _client_with_transport(handler) -> MakerWorldClient:
    client = MakerWorldClient(auth_token="tok")
    # swap the transport on the existing client (keeps headers intact)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "test"},
        follow_redirects=True,
    )
    return client


@pytest.mark.asyncio
async def test_mw_get_401_maps_to_expired():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 4})

    client = _client_with_transport(handler)
    try:
        with __import__("pytest").raises(_exc("AuthExpiredError")):
            await client._mw_get("/api/x")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mw_get_403_please_log_in_maps_to_auth_required():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "Please log in to download models"})

    client = _client_with_transport(handler)
    try:
        with __import__("pytest").raises(_exc("AuthRequiredError")):
            await client._mw_get("/api/x")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mw_get_403_other_maps_to_forbidden():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "no rights"})

    client = _client_with_transport(handler)
    try:
        with __import__("pytest").raises(_exc("ForbiddenError")):
            await client._mw_get("/api/x")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mw_get_404_maps_to_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client_with_transport(handler)
    try:
        with __import__("pytest").raises(_exc("NotFoundError")):
            await client._mw_get("/api/x")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mw_get_captcha_body_maps_to_captcha():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "captcha"})

    client = _client_with_transport(handler)
    try:
        with __import__("pytest").raises(_exc("CaptchaError")):
            await client._mw_get("/api/x")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mw_get_requires_token_when_auth_true():
    client = MakerWorldClient()  # no token
    try:
        with __import__("pytest").raises(_exc("AuthRequiredError")):
            await client._mw_get("/api/x", auth=True)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mw_get_auth_header_attached():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(handler)
    try:
        data = await client._mw_get("/api/x", auth=True)
        assert data == {"ok": True}
        assert captured["auth"] == "Bearer tok"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mw_get_text_plain_json_parsed():
    """Gateway sometimes labels JSON as text/plain — must parse leniently."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"ok": true}',
            headers={
                "content-type": "text/plain",
            },
        )

    client = _client_with_transport(handler)
    try:
        assert await client._mw_get("/api/x") == {"ok": True}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_my_collections_enriches_and_walks():
    """listlite listing + per-collection enrichment (slug + design ids).

    listlite (the verified personal-collections endpoint) returns all
    collections at once with NO slug and NO designs; both come from
    per-collection calls (withoutdesign / designs pager).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/my/favorites/listlite"):
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "default": {"id": 2, "title": "B"},
                    "hits": [
                        {"id": 1, "title": "A", "designCnt": 2, "isDefault": False},
                        {"id": 2, "title": "B", "designCnt": 1, "isDefault": True},
                    ],
                },
            )
        if path.endswith("/favorites/1/withoutdesign"):
            return httpx.Response(200, json={"title": "A", "slug": "a"})
        if path.endswith("/favorites/2/withoutdesign"):
            return httpx.Response(200, json={"title": "B", "slug": "b"})
        if path.endswith("/favorites/1/designs"):
            offset = int(request.url.params["offset"])
            pages = {0: [{"id": 11}, {"id": 12}], 2: []}
            return httpx.Response(200, json={"total": 2, "hits": pages[offset]})
        if path.endswith("/favorites/2/designs"):
            offset = int(request.url.params["offset"])
            pages = {0: [{"id": 13}]}
            return httpx.Response(200, json={"total": 1, "hits": pages[offset]})
        return httpx.Response(404)

    client = _client_with_transport(handler)
    try:
        mine = await client.list_my_collections()
        assert mine == [
            {
                "collection_id": 1,
                "title": "A",
                "slug": "a",
                "design_count": 2,
                "is_default": False,
                "design_ids": [11, 12],
            },
            {
                "collection_id": 2,
                "title": "B",
                "slug": "b",
                "design_count": 1,
                "is_default": True,
                "design_ids": [13],
            },
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_my_collections_survives_enrichment_failure():
    """A failing slug/ids lookup for one collection keeps the listing alive
    (title still shown, slug empty, design ids missing) — partial data beats
    losing the whole listing."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/my/favorites/listlite"):
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "hits": [
                        {"id": 1, "title": "A", "designCnt": 5000, "isDefault": False}
                    ],
                },
            )
        return httpx.Response(500)

    client = _client_with_transport(handler)
    try:
        mine = await client.list_my_collections()
        assert mine == [
            {
                "collection_id": 1,
                "title": "A",
                "slug": "",
                "design_count": 5000,
                "is_default": False,
                "design_ids": [],
            }
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_my_collections_design_cap():
    """design_ids are capped at max_designs_per_collection (5000-model
    collection with cap 1000 must not page forever)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/my/favorites/listlite"):
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "hits": [
                        {"id": 1, "title": "A", "designCnt": 5000, "isDefault": False}
                    ],
                },
            )
        if path.endswith("/favorites/1/withoutdesign"):
            return httpx.Response(200, json={"title": "A", "slug": "a"})
        if path.endswith("/favorites/1/designs"):
            offset = int(request.url.params["offset"])
            # 10+ pages of 100 designs each; total 5000 so only cap/loop-guard stops it
            ids = [{"id": 1000 + offset + i} for i in range(100)]
            return httpx.Response(200, json={"total": 5000, "hits": ids})
        return httpx.Response(404)

    client = _client_with_transport(handler)
    try:
        mine = await client.list_my_collections(max_designs_per_collection=1000)
        ids = mine[0]["design_ids"]
        assert len(ids) == 1000
        assert mine[0]["slug"] == "a"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_validate_token_tri_state():
    statuses = {"ok": 200, "expired": 401, "ambiguous": 503}
    for name, status in statuses.items():

        def handler(request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status)

        client = _client_with_transport(handler)
        try:
            result = await client.validate_token("t")
        finally:
            await client.close()
        expected = {"ok": True, "expired": False, "ambiguous": None}[name]
        assert result is expected, name


# -------------------------------------------------------------- client pool
@pytest.mark.asyncio
async def test_client_pool_reuse_and_invalidation():
    await invalidate_shared_clients()
    a = get_client(auth_token="t1", region="global")
    b = get_client(auth_token="t1", region="global")
    assert a is b
    c = get_client(auth_token="t2", region="global")
    assert c is not a
    # release keeps pooled clients open
    await release_client(a)
    assert not a._client.is_closed
    # ad-hoc clients are closed on release
    adhoc = MakerWorldClient()
    await release_client(adhoc)
    assert adhoc._client.is_closed
    # invalidation closes all and empties the pool
    await invalidate_shared_clients()
    assert a._client.is_closed
    assert c._client.is_closed
    d = get_client(auth_token="t1", region="global")
    assert d is not a
    await invalidate_shared_clients()


# --------------------------------------------------------- download routing
@pytest.mark.asyncio
async def test_download_file_routes_s3_to_verbatim():
    """amazonaws.com URLs must route through the verbatim path (thread-offloaded).

    Regression guard: _download_s3_verbatim must be a SYNC function — it
    runs via asyncio.to_thread, which would otherwise return the un-awaited
    coroutine object instead of (size, filename).
    """
    import inspect
    from pathlib import Path

    assert not inspect.iscoroutinefunction(MakerWorldClient._download_s3_verbatim)

    captured = {}
    sentinel = (4, "model.3mf")

    def fake_verbatim(self, url, dest_path):
        captured["url"] = url
        captured["dest"] = str(dest_path)
        return sentinel

    client = MakerWorldClient()
    original = MakerWorldClient._download_s3_verbatim
    MakerWorldClient._download_s3_verbatim = fake_verbatim
    dest = Path("/tmp/bnd_test_s3.3mf")
    try:
        result = await client.download_file(
            "https://bucket.s3.amazonaws.com/model.3mf?X-Amz-Signature=abc", dest
        )
        assert result == sentinel
        assert "amazonaws.com" in captured["url"]
    finally:
        MakerWorldClient._download_s3_verbatim = original


@pytest.mark.asyncio
async def test_download_file_cdn_path_uses_httpx_stream():
    """CDN (non-S3) URLs go through the async httpx path."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers  # signed URL IS the credential
        return httpx.Response(
            200,
            content=b"12345678",
            headers={"content-disposition": 'attachment; filename="m.3mf"'},
        )

    client = MakerWorldClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from pathlib import Path

    dest = Path("/tmp/bnd_test_cdn.3mf")
    try:
        size, name = await client.download_file(
            "https://makerworld.bblmw.com/x/m", dest
        )
        assert size == 8
        assert name == "m.3mf"
        assert dest.read_bytes() == b"12345678"
    finally:
        dest.unlink(missing_ok=True)
        await client.close()


def test_cap_constants_cover_max_design_cap():
    """Pager ceiling must cover the 1000-design cap (pages of up to 64)."""
    assert mw.CAP_MAX_PAGES * mw.CAP_PAGE_SIZE >= 1000


@pytest.mark.asyncio
async def test_collect_design_ids_paging_and_cap(monkeypatch):
    """_collect_design_ids walks pages, dedups known ids, respects cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        # 10+ pages of 100 designs each; total 5000 so only cap/loop-guard stops it
        ids = [{"id": 1000 + offset + i} for i in range(100)]
        return httpx.Response(200, json={"total": 5000, "hits": ids})

    client = MakerWorldClient(auth_token="tok")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "test"},
        follow_redirects=True,
    )
    # kill the politeness delay for tests
    monkeypatch.setattr(mw.settings, "download_delay_seconds", 0.0)
    try:
        known = {1000, 1001}  # embedded-page overlap
        extra = await client._collect_design_ids(1, known=known, cap=5)
        assert len(extra) == 3  # 2 known + 3 new == cap 5
        assert all(x not in known for x in extra)
        # cap=0 edge: nothing may be collected
        extra0 = await client._collect_design_ids(1, known=set(), cap=0)
        assert extra0 == []
    finally:
        await client.close()
