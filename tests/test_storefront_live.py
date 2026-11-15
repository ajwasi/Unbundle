"""Opt-in canary against the REAL humblebundle.com.

Skipped in normal runs (the suite makes no network calls). Run it to find out
whether Humble has changed its markup since the fixtures were recorded:

    RUN_LIVE_TESTS=1 pytest tests/test_storefront_live.py

.github/workflows/source-canary.yml runs it weekly, so a markup change shows
up as a red scheduled run — before a user sees a Home page notice.
"""

import os

import pytest

import app.connectors.storefront as storefront

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="live network test; set RUN_LIVE_TESTS=1 to run"
)


@pytest.fixture(autouse=True)
def _fresh_caches():
    storefront._listing_cache = None
    storefront._detail_cache = {}


@pytest.mark.asyncio
async def test_live_listing_and_a_live_bundle_page_still_parse():
    bundles = await storefront.fetch_current_bundles(force=True)

    assert len(bundles) >= 5, "Humble's bundle listing parsed to almost nothing — has the markup changed?"
    assert {b.category for b in bundles} >= {"books", "games"}
    assert all(b.machine_name and b.name and b.product_url and b.end_date for b in bundles)

    detail = await storefront.fetch_bundle_detail(bundles[0].product_url, force=True)
    assert detail.name and detail.items and detail.tiers
    assert all(i.machine_name and i.name for i in detail.items)
