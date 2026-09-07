import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.connectors.storefront as storefront


@pytest.fixture(autouse=True)
def _reset_caches():
    # Module-level TTL caches must not leak between tests.
    storefront._listing_cache = None
    storefront._detail_cache = {}
    yield
    storefront._listing_cache = None
    storefront._detail_cache = {}


def _listing_html(products_by_category: dict) -> str:
    data = {
        cat: {"mosaic": [{"products": products}]}
        for cat, products in products_by_category.items()
    }
    payload = {"data": data}
    return f'<html><script id="landingPage-json-data" type="application/json">{json.dumps(payload)}</script></html>'


def _detail_html(bundle_data: dict) -> str:
    payload = {"bundleData": bundle_data}
    return f'<html><script id="webpack-bundle-page-data" type="application/json">{json.dumps(payload)}</script></html>'


def _product(
    name="Some Bundle",
    machine_name="somebundle",
    product_url="/games/some-bundle",
    category="bundle",
    start_date="2026-09-01T18:00:00",
    end_date="2026-09-22T18:00:00",
):
    product = {
        "machine_name": machine_name,
        "tile_name": name,
        "marketing_blurb": "Get <em>Cool Thing</em>!",
        "product_url": product_url,
        "tile_image": "https://hb.imgix.net/img.jpg",
        "category": category,
    }
    if start_date is not None:
        product["start_date|datetime"] = start_date
    if end_date is not None:
        product["end_date|datetime"] = end_date
    return product


@pytest.mark.asyncio
async def test_fetch_current_bundles_parses_all_three_categories():
    html = _listing_html({"games": [_product("Game Bundle", "gb", "/games/gb")], "books": [_product("Book Bundle", "bb", "/books/bb")], "software": []})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert {b.category for b in bundles} == {"games", "books"}
    game = next(b for b in bundles if b.category == "games")
    assert game.name == "Game Bundle"
    assert game.machine_name == "gb"
    assert game.product_url == "https://www.humblebundle.com/games/gb"
    assert "<em>Cool Thing</em>" in game.blurb


@pytest.mark.asyncio
async def test_fetch_current_bundles_sanitizes_blurb_markup():
    # blurb renders with autoescaping bypassed (see home/_bundles.html), so a
    # compromised or malicious listing must not be able to inject script/markup
    # through it even though this field is otherwise trusted.
    # Note: deliberately no literal "</script>" substring in the payload below —
    # that would prematurely terminate the *test fixture's* naive regex-based
    # extraction of the surrounding <script id="landingPage-json-data"> block
    # (a test-harness limitation, unrelated to nh3's real handling of <script>,
    # which was separately confirmed interactively).
    product = _product()
    product["marketing_blurb"] = 'Buy <a href="javascript:alert(1)">now</a><img src=x onerror=alert(1)>!'
    html = _listing_html({"games": [product]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    blurb = bundles[0].blurb
    assert "javascript:" not in blurb
    assert "onerror" not in blurb
    assert "<a" not in blurb and "<img" not in blurb
    assert "Buy" in blurb and "now" in blurb  # stripped tags still keep their visible text


@pytest.mark.asyncio
async def test_fetch_current_bundles_parses_start_and_end_dates():
    from datetime import datetime

    html = _listing_html({"games": [_product(start_date="2026-08-26T18:00:00", end_date="2026-09-17T04:00:00")]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert bundles[0].start_date == datetime(2026, 8, 26, 18, 0, 0)
    assert bundles[0].end_date == datetime(2026, 9, 17, 4, 0, 0)


@pytest.mark.asyncio
async def test_fetch_current_bundles_handles_missing_dates():
    html = _listing_html({"games": [_product(start_date=None, end_date=None)]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert bundles[0].start_date is None
    assert bundles[0].end_date is None


@pytest.mark.asyncio
async def test_fetch_current_bundles_handles_malformed_dates():
    html = _listing_html({"games": [_product(end_date="not-a-real-date")]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert bundles[0].end_date is None  # parsed defensively, never raises


@pytest.mark.asyncio
async def test_fetch_current_bundles_skips_non_bundle_products():
    html = _listing_html({"games": [_product(category="storefront")]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert bundles == []


@pytest.mark.asyncio
async def test_fetch_current_bundles_raises_when_marker_missing():
    resp = httpx.Response(200, text="<html>no data here</html>", request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError):
            await storefront.fetch_current_bundles()


@pytest.mark.asyncio
async def test_fetch_current_bundles_uses_cache_within_ttl():
    html = _listing_html({"games": [_product()]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await storefront.fetch_current_bundles()
        await storefront.fetch_current_bundles()
    assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_fetch_current_bundles_force_bypasses_cache():
    html = _listing_html({"games": [_product()]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await storefront.fetch_current_bundles()
        await storefront.fetch_current_bundles(force=True)
    assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_current_bundles_refetches_after_ttl_expires():
    html = _listing_html({"games": [_product()]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await storefront.fetch_current_bundles()
        storefront._listing_cache = (storefront._listing_cache[0] - storefront.LISTING_TTL_SECONDS - 1, storefront._listing_cache[1])
        await storefront.fetch_current_bundles()
    assert mock_get.call_count == 2


def _tier_item(machine_name, human_name, content_type="ebook", price=10.0):
    return {"human_name": human_name, "item_content_type": content_type, "min_price|money": {"amount": price}}


@pytest.mark.asyncio
async def test_fetch_bundle_detail_rejects_a_url_on_another_host():
    # Defense-in-depth: home.py's /storefront/compare route already validates the
    # host before ever calling this, but this connector must not blindly trust an
    # arbitrary caller (or a future one) to have done that.
    with patch("httpx.AsyncClient.get", new=AsyncMock()) as mock_get:
        with pytest.raises(ValueError, match="outside"):
            await storefront.fetch_bundle_detail("https://evil.example.com/books/x")
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_bundle_detail_rejects_non_https_scheme():
    with patch("httpx.AsyncClient.get", new=AsyncMock()) as mock_get:
        with pytest.raises(ValueError, match="outside"):
            await storefront.fetch_bundle_detail("http://www.humblebundle.com/books/x")
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_bundle_detail_parses_name_and_msrp():
    bundle_data = {
        "basic_data": {"human_name": "My Bundle", "msrp|money": {"amount": 100.0}},
        "tier_item_data": {},
        "tier_order": [],
        "tier_display_data": {},
        "tier_pricing_data": {},
    }
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x")
    assert detail.name == "My Bundle"
    assert detail.msrp_total == 100.0


@pytest.mark.asyncio
async def test_fetch_bundle_detail_filters_out_charity_tip_items():
    # A charity/EFF-style tile has a human_name but no item_content_type — not a
    # real product, must not appear as a comparable item.
    bundle_data = {
        "basic_data": {"human_name": "B"},
        "tier_item_data": {
            "eff": {"human_name": "Electronic Frontier Foundation", "item_content_type": None},
            "realbook": _tier_item("realbook", "Real Book"),
        },
        "tier_order": [],
        "tier_display_data": {},
        "tier_pricing_data": {},
    }
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x")
    assert [i.machine_name for i in detail.items] == ["realbook"]


@pytest.mark.asyncio
async def test_fetch_bundle_detail_tier_diffing_shows_only_newly_unlocked_items():
    # Confirmed against a real 3-tier bundle: tier_item_machine_names is
    # CUMULATIVE (each tier repeats every lower tier's items plus its own new
    # ones) — each StorefrontTier must only list what's new at that price.
    bundle_data = {
        "basic_data": {"human_name": "Tiered Bundle"},
        "tier_item_data": {
            "a": _tier_item("a", "Item A"),
            "b": _tier_item("b", "Item B"),
            "c": _tier_item("c", "Item C"),
        },
        "tier_order": ["bt25", "initial"],  # highest-price-first, as Humble returns it
        "tier_display_data": {
            "initial": {"header": "Pay $5 to unlock!", "tier_item_machine_names": ["a"], "bonus_item_machine_names": []},
            "bt25": {
                "header": "Pay $25 or more to also unlock!",
                "tier_item_machine_names": ["a", "b", "c"],
                "bonus_item_machine_names": [],
            },
        },
        "tier_pricing_data": {
            "initial": {"price|money": {"amount": 5.0}, "is_bta": False},
            "bt25": {"price|money": {"amount": 25.0}, "is_bta": False},
        },
    }
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x")

    assert [t.identifier for t in detail.tiers] == ["initial", "bt25"]  # reordered low-to-high
    assert [i.machine_name for i in detail.tiers[0].items] == ["a"]
    assert [i.machine_name for i in detail.tiers[1].items] == ["b", "c"]  # "a" not repeated
    assert detail.tiers[0].price_amount == 5.0
    assert detail.tiers[1].price_amount == 25.0


@pytest.mark.asyncio
async def test_fetch_bundle_detail_skips_tier_with_nothing_new():
    # A tier whose only "new" item was the already-filtered-out charity tile
    # should not produce an empty tier card.
    bundle_data = {
        "basic_data": {"human_name": "B"},
        "tier_item_data": {"a": _tier_item("a", "Item A"), "eff": {"human_name": "EFF", "item_content_type": None}},
        "tier_order": ["bt25", "initial"],
        "tier_display_data": {
            "initial": {"header": "Pay $5", "tier_item_machine_names": ["a"], "bonus_item_machine_names": []},
            "bt25": {"header": "Pay $25", "tier_item_machine_names": ["a", "eff"], "bonus_item_machine_names": []},
        },
        "tier_pricing_data": {
            "initial": {"price|money": {"amount": 5.0}, "is_bta": False},
            "bt25": {"price|money": {"amount": 25.0}, "is_bta": False},
        },
    }
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x")
    assert [t.identifier for t in detail.tiers] == ["initial"]
