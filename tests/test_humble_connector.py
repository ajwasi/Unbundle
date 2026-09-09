from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import settings
from app.connectors.base import ConnectorAuthError
from app.connectors.humble_connector import BASE_URL, HumbleConnector, _base_url, order_page_url, parse_bundle
from tests.factories import make_order, make_subproduct, make_tpk


def test_base_url_is_real_by_default():
    assert _base_url() == BASE_URL


def test_base_url_redirects_to_the_mock_server_in_demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "mock_api_base_url", "http://127.0.0.1:9999")
    assert _base_url() == "http://127.0.0.1:9999/humble/api/v1"


def test_order_page_url_builds_the_real_downloads_key_pattern():
    assert order_page_url("GK1") == "https://www.humblebundle.com/downloads?key=GK1"


def test_parse_bundle_basic_fields():
    order = make_order(name="My Bundle", category="bundle", created="2024-03-01T12:00:00", amount_spent=15.5)
    normalized = parse_bundle("GK1", order)
    assert normalized.gamekey == "GK1"
    assert normalized.name == "My Bundle"
    assert normalized.category == "bundle"
    assert normalized.purchased_at == "2024-03-01T12:00:00"
    assert normalized.amount_spent == 15.5


def test_parse_bundle_falls_back_to_gamekey_when_no_product_name():
    order = make_order()
    del order["product"]["human_name"]
    normalized = parse_bundle("GK2", order)
    assert normalized.name == "GK2"


def test_parse_bundle_extracts_download_variants():
    order = make_order(
        subproducts=[
            make_subproduct(
                human_name="Cool Book",
                downloads=[
                    {
                        "download_struct": [
                            {"name": "EPUB", "file_size": 1000, "url": {"web": "https://dl.humble.com/book.epub?x=1"}},
                            {"name": "PDF", "file_size": 2000, "url": {"web": "https://dl.humble.com/book.pdf?x=1"}},
                        ]
                    }
                ],
            )
        ]
    )
    normalized = parse_bundle("GK3", order)
    assert len(normalized.downloads) == 2
    epub = next(d for d in normalized.downloads if d.file_format == "EPUB")
    assert epub.item_name == "Cool Book"
    assert epub.subproduct_index == 1
    assert epub.original_filename == "book.epub"
    assert epub.expected_size_bytes == 1000
    assert epub.source_url == "https://dl.humble.com/book.epub?x=1"


def test_parse_bundle_download_items_carry_the_subproducts_machine_name():
    # catalog.py already keys ItemTag on this same subproduct field — downloads
    # need it too so a completed download can resolve its own item-level
    # routing tag (app/downloads/relocate.py).
    order = make_order(subproducts=[make_subproduct(human_name="Cool Book", machine_name="coolbook123")])
    normalized = parse_bundle("GK9", order)
    assert normalized.downloads[0].machine_name == "coolbook123"


def test_parse_bundle_skips_variants_with_no_url():
    order = make_order(
        subproducts=[
            make_subproduct(downloads=[{"download_struct": [{"name": "EPUB", "file_size": 1000, "url": {}}]}])
        ]
    )
    normalized = parse_bundle("GK4", order)
    assert normalized.downloads == []


def test_parse_bundle_subproduct_index_is_1_based_and_includes_empty_subproducts():
    # A subproduct with zero downloads (e.g. a beta-key placeholder) still occupies
    # a real slot in humble-cli's -i index space — confirmed against the real binary.
    order = make_order(
        subproducts=[
            make_subproduct(human_name="Placeholder", downloads=[]),
            make_subproduct(human_name="Real Item"),
        ]
    )
    normalized = parse_bundle("GK5", order)
    assert len(normalized.downloads) == 1
    assert normalized.downloads[0].item_name == "Real Item"
    assert normalized.downloads[0].subproduct_index == 2


def test_parse_bundle_item_count_includes_zero_download_subproducts():
    order = make_order(subproducts=[make_subproduct(downloads=[]), make_subproduct()])
    normalized = parse_bundle("GK6", order)
    assert normalized.item_count == 2


def test_parse_bundle_extracts_entitlements():
    order = make_order(
        tpks=[
            make_tpk(human_name="Half-Life 2 - Steam", machine_name="hl2", keyindex=0, redeemed=True, steam_app_id="220"),
            make_tpk(human_name="Portal - Steam", machine_name="portal", keyindex=0, redeemed=False),
        ]
    )
    normalized = parse_bundle("GK7", order)
    assert len(normalized.entitlements) == 2
    hl2 = next(e for e in normalized.entitlements if e.machine_name == "hl2")
    assert hl2.key_name == "Half-Life 2 - Steam"
    assert hl2.redeemed_on_source is True
    assert hl2.steam_app_id == "220"
    portal = next(e for e in normalized.entitlements if e.machine_name == "portal")
    assert portal.redeemed_on_source is False
    assert portal.steam_app_id is None


def test_parse_bundle_entitlements_never_appear_as_downloads():
    order = make_order(subproducts=[], tpks=[make_tpk()])
    normalized = parse_bundle("GK8", order)
    assert normalized.downloads == []
    assert len(normalized.entitlements) == 1


@pytest.mark.asyncio
async def test_check_credentials_ok():
    resp = httpx.Response(200, json=[{"gamekey": "a"}], request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await HumbleConnector({"session_key": "abc"}).check_credentials()
    assert result.ok


@pytest.mark.asyncio
async def test_check_credentials_rejects_missing_session_key():
    result = await HumbleConnector({}).check_credentials()
    assert not result.ok


@pytest.mark.asyncio
async def test_check_credentials_rejects_401():
    resp = httpx.Response(401, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await HumbleConnector({"session_key": "abc"}).check_credentials()
    assert not result.ok


@pytest.mark.asyncio
async def test_check_credentials_rejects_html_response_as_expired_session():
    # An expired cookie can come back 200 with an HTML login page rather than a
    # clean 401 — must be caught via content-type, not status code alone.
    resp = httpx.Response(200, content=b"<html>login</html>", headers={"content-type": "text/html"}, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await HumbleConnector({"session_key": "abc"}).check_credentials()
    assert not result.ok


@pytest.mark.asyncio
async def test_sync_uses_repeated_gamekeys_query_params_not_comma_joined():
    order_list_resp = httpx.Response(200, json=[{"gamekey": "a"}, {"gamekey": "b"}], request=httpx.Request("GET", "https://x"))
    orders_resp = httpx.Response(200, json={"a": make_order(), "b": make_order()}, request=httpx.Request("GET", "https://x"))

    calls = []

    async def fake_get(self, url, params=None, headers=None):
        calls.append((url, params))
        return order_list_resp if url.endswith("/user/order") else orders_resp

    with patch("httpx.AsyncClient.get", new=fake_get):
        bundles = await HumbleConnector({"session_key": "abc"}).sync(lambda level, msg: None)

    assert len(bundles) == 2
    orders_call = next(c for c in calls if c[0].endswith("/orders"))
    # Must be a list (httpx serializes lists as repeated params) — a single
    # comma-joined string was confirmed to silently break Humble's real API.
    assert orders_call[1]["gamekeys"] == ["a", "b"]


@pytest.mark.asyncio
async def test_sync_raises_connector_auth_error_on_401():
    resp = httpx.Response(401, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ConnectorAuthError):
            await HumbleConnector({"session_key": "abc"}).sync(lambda level, msg: None)


@pytest.mark.asyncio
async def test_sync_skips_malformed_bundle_without_aborting():
    order_list_resp = httpx.Response(200, json=[{"gamekey": "a"}, {"gamekey": "b"}], request=httpx.Request("GET", "https://x"))
    # "a" is malformed (subproducts is a string, not a list) — must not abort "b".
    orders_resp = httpx.Response(
        200, json={"a": {"subproducts": "not-a-list", "product": {}}, "b": make_order()}, request=httpx.Request("GET", "https://x")
    )

    async def fake_get(self, url, params=None, headers=None):
        return order_list_resp if url.endswith("/user/order") else orders_resp

    logged = []
    with patch("httpx.AsyncClient.get", new=fake_get):
        bundles = await HumbleConnector({"session_key": "abc"}).sync(lambda level, msg: logged.append((level, msg)))

    assert len(bundles) == 1
    assert any("failed to parse bundle a" in msg for _, msg in logged)
