"""Contract tests: the storefront parser against REAL recorded Humble pages.

tests/fixtures/humble/*.html are trimmed copies of what humblebundle.com
actually served (see scripts/record_humble_fixtures.py), not hand-built
approximations — the synthetic pages in test_storefront_connector.py prove the
parser's logic, these prove it still matches Humble's real shape. They assert
the *shape* (required fields exist and look right), never specific titles or
prices, so re-recording never breaks them unless Humble genuinely changed
something the parser depends on.

When Humble changes its markup: run the recorder, watch these fail with a
readable message, fix the parser, done — rather than finding out from a 500.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.connectors.storefront as storefront

FIXTURES = Path(__file__).parent / "fixtures" / "humble"

LISTING_KEYS = {"productKey", "slug", "url", "title", "thumbnail", "endDate", "category", "productType"}
PAGE_KEYS = {"title", "bundleType", "value", "tiers"}
TIER_KEYS = {"sku", "price", "bundleItems"}
ITEM_KEYS = {"key", "title"}


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf8")


def _raw_hits(html: str) -> list[dict]:
    marker = '{"relationTo":"algolia-hit","value":'
    return [h for h in storefront._objects_after(storefront._flight_text(html), marker) if isinstance(h, dict)]


def _raw_page(html: str) -> dict:
    return next(
        p
        for p in storefront._objects_after(storefront._flight_text(html), '"bundlePage":')
        if isinstance(p, dict) and "tiers" in p
    )


@pytest.fixture(autouse=True)
def _reset_caches():
    storefront._listing_cache = None
    storefront._detail_cache = {}
    yield
    storefront._listing_cache = None
    storefront._detail_cache = {}


def _mock_get(html: str):
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    return patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp))


# --------------------------------------------------------------------- listing


def test_the_recorded_listing_still_carries_every_field_the_parser_reads():
    hits = _raw_hits(_html("listing.html"))
    assert hits, "no algolia-hit records found in the recorded listing"
    for hit in hits:
        missing = LISTING_KEYS - set(hit)
        assert not missing, f"listing record {hit.get('slug')!r} lost fields {sorted(missing)}"


@pytest.mark.asyncio
async def test_the_recorded_listing_parses_into_complete_bundles():
    with _mock_get(_html("listing.html")):
        bundles = await storefront.fetch_current_bundles(force=True)

    assert {b.category for b in bundles} == {"books", "games", "software"}
    for b in bundles:
        assert b.machine_name, b
        assert b.name, b
        assert b.product_url.startswith("https://www.humblebundle.com/"), b
        assert b.image_url.startswith("https://"), b
        assert isinstance(b.end_date, datetime) and b.end_date.tzinfo is None, b  # naive UTC, like the legacy parser
        assert b.blurb.startswith("Includes "), b


# ---------------------------------------------------------------------- detail


@pytest.mark.parametrize("fixture", ["detail_books.html", "detail_games.html"])
def test_a_recorded_bundle_page_still_carries_every_field_the_parser_reads(fixture):
    page = _raw_page(_html(fixture))
    assert not PAGE_KEYS - set(page), f"bundlePage lost fields {sorted(PAGE_KEYS - set(page))}"
    assert page["tiers"], "no tiers"
    for tier in page["tiers"]:
        assert not TIER_KEYS - set(tier), f"tier {tier.get('id')} lost fields {sorted(TIER_KEYS - set(tier))}"
        for entry in tier["bundleItems"]:
            if isinstance(entry, dict):  # strings are back-references to lower tiers' items
                assert not ITEM_KEYS - set(entry), f"item lost fields {sorted(ITEM_KEYS - set(entry))}"


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["detail_books.html", "detail_games.html"])
async def test_a_recorded_bundle_page_parses_into_priced_tiers_of_owned_style_items(fixture):
    html = _html(fixture)
    bundle_key = storefront._bundle_key(_raw_page(html))
    assert bundle_key, "bundle key could not be recovered from the tier skus"

    with _mock_get(html):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/x/y", force=True)

    assert detail.name
    assert detail.msrp_total and detail.msrp_total > 0
    assert detail.items and detail.tiers

    prices = [t.price_amount for t in detail.tiers]
    assert all(p is not None and p > 0 for p in prices) and prices == sorted(prices)
    assert sum(len(t.items) for t in detail.tiers) == len(detail.items)

    for item in detail.items:
        assert item.name and item.machine_name
        # The key Humble publishes is "<item>_<bundle key>"; owned orders use the bare item name.
        assert not item.machine_name.endswith("_" + bundle_key), item.machine_name
        assert item.image_url.startswith("https://"), item
        assert item.publishers or item.authors, item
