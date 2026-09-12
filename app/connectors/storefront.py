"""Storefront connector — reads Humble Bundle's currently-for-sale listings
from the public marketing site. This is deliberately separate from
humble_connector.py: it's unauthenticated (no session cookie, no /api/v1),
scrapes embedded JSON off the public HTML instead of calling an API, and
describes bundles for sale rather than bundles already owned.

Both the listing page and individual bundle pages embed their full data as a
<script type="application/json"> block. Confirmed reachable via a plain GET
with a browser User-Agent — unlike /processlogin, these pages are not behind
Cloudflare's bot check, so no browser automation is needed here.

In-memory TTL cache only. This is public marketing data with no per-user
meaning, so there's nothing worth persisting across restarts, and it goes
stale (bundles rotate) on its own regardless.
"""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx
import nh3

BASE_URL = "https://www.humblebundle.com"
# marketing_blurb renders with autoescaping bypassed (see home/_bundles.html) so
# Humble's own <em>-style emphasis shows up as intended — sanitized here rather
# than trusted outright, so a compromised or malicious listing can't inject
# markup/JS through it. Real blurbs confirmed to use at least <em>; the rest of
# this allowlist is the plausible-and-harmless rest of that same family, no
# attributes on any of them (closes off href="javascript:..." / onerror=... etc.
# by construction, not by trying to enumerate bad attributes).
_ALLOWED_BLURB_TAGS = {"em", "strong", "b", "i", "br"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
LISTING_TTL_SECONDS = 1800
DETAIL_TTL_SECONDS = 1800

_LISTING_MARKER = re.compile(
    r'<script id="landingPage-json-data" type="application/json">(.*?)</script>', re.S
)
_DETAIL_MARKER = re.compile(
    r'<script id="webpack-bundle-page-data" type="application/json">(.*?)</script>', re.S
)


@dataclass
class StorefrontBundle:
    category: str  # books | games | software
    machine_name: str
    name: str
    blurb: str
    product_url: str  # full https:// URL
    image_url: str
    # Confirmed present on all 57 live listings (2026-09-07); no timezone marker
    # in Humble's own data — treated as UTC, unverified. Defaulted to None (not
    # required positionally) so existing construction sites/tests that don't
    # care about dates aren't forced to specify them.
    start_date: datetime | None = None
    end_date: datetime | None = None


@dataclass
class StorefrontItem:
    machine_name: str
    name: str
    content_type: str | None
    msrp_amount: float | None


@dataclass
class StorefrontTier:
    identifier: str
    label: str  # Humble's own marketing header, e.g. "Pay $25 or more to also unlock!"
    price_amount: float | None
    is_bta: bool  # "beat the average" — price_amount is a snapshot, not fixed
    items: list[StorefrontItem]  # only the items newly unlocked at this tier, not lower tiers' items again


@dataclass
class StorefrontBundleDetail:
    name: str
    msrp_total: float | None
    items: list[StorefrontItem]
    tiers: list[StorefrontTier]  # lowest price first


_listing_cache: tuple[float, list[StorefrontBundle]] | None = None
_detail_cache: dict[str, tuple[float, StorefrontBundleDetail]] = {}
# Wall-clock companion to _listing_cache's monotonic timestamp (monotonic time
# isn't meaningful to show a user) — set alongside it, purely for the "last
# pulled" display on the Home page; never read for TTL/cache-expiry logic.
_listing_fetched_at: datetime | None = None


def last_fetched_at() -> datetime | None:
    """When the storefront listing was last actually fetched (not merely
    requested — a cache hit doesn't move this). None before the first fetch
    of this process's lifetime.
    """
    return _listing_fetched_at


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def fetch_current_bundles(force: bool = False) -> list[StorefrontBundle]:
    global _listing_cache, _listing_fetched_at
    if _listing_cache and not force and time.monotonic() - _listing_cache[0] < LISTING_TTL_SECONDS:
        return _listing_cache[1]

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(f"{BASE_URL}/bundles")
        resp.raise_for_status()

    match = _LISTING_MARKER.search(resp.text)
    if not match:
        raise ValueError("Could not find landingPage-json-data on the bundles listing page")
    data = (json.loads(match.group(1)).get("data")) or {}

    bundles: list[StorefrontBundle] = []
    for category, section in data.items():
        for group in section.get("mosaic") or []:
            for product in group.get("products") or []:
                if product.get("category") != "bundle":
                    continue
                product_url = product.get("product_url") or ""
                bundles.append(
                    StorefrontBundle(
                        category=category,
                        machine_name=product.get("machine_name") or "",
                        name=product.get("tile_name") or product.get("tile_short_name") or "",
                        blurb=nh3.clean(product.get("marketing_blurb") or "", tags=_ALLOWED_BLURB_TAGS, attributes={}),
                        product_url=f"{BASE_URL}{product_url}" if product_url else "",
                        image_url=product.get("tile_image") or "",
                        start_date=_parse_datetime(product.get("start_date|datetime")),
                        end_date=_parse_datetime(product.get("end_date|datetime")),
                    )
                )

    _listing_cache = (time.monotonic(), bundles)
    _listing_fetched_at = datetime.utcnow()
    return bundles


async def fetch_bundle_detail(product_url: str, force: bool = False) -> StorefrontBundleDetail:
    cached = _detail_cache.get(product_url)
    if cached and not force and time.monotonic() - cached[0] < DETAIL_TTL_SECONDS:
        return cached[1]

    # Three prior attempts at validating product_url in place — a caller-side check
    # in home.py; a guard-clause-with-early-raise here; the sink moved inside the
    # true branch of a direct literal comparison — all still left CodeQL's SSRF
    # query flagging client.get() below. Validating a tainted string doesn't clear a
    # direct source-to-sink dataflow edge in its model, no matter how the validation
    # is shaped, as long as that same tainted string is still what's passed to the
    # sink. This is CodeQL's own documented fix for py/full-ssrf instead: never let
    # the sink see the original tainted value at all. Rebuild the request URL from
    # BASE_URL (a trusted literal) plus only the path/query/fragment pulled out of
    # product_url — the scheme+host that actually determines *destination* is now
    # always the literal string, never attacker-influenced.
    parsed = urlparse(product_url)
    if parsed.scheme != "https" or parsed.hostname != "www.humblebundle.com":
        raise ValueError(f"Refusing to fetch a bundle detail URL outside {BASE_URL}")
    safe_url = f"{BASE_URL}{parsed.path}"
    if parsed.query:
        safe_url = f"{safe_url}?{parsed.query}"
    if parsed.fragment:
        safe_url = f"{safe_url}#{parsed.fragment}"

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(safe_url)
        resp.raise_for_status()

    match = _DETAIL_MARKER.search(resp.text)
    if not match:
        raise ValueError(f"Could not find webpack-bundle-page-data on {product_url}")
    bundle_data = (json.loads(match.group(1)).get("bundleData")) or {}
    basic_data = bundle_data.get("basic_data") or {}
    tier_items = bundle_data.get("tier_item_data") or {}

    items: list[StorefrontItem] = []
    items_by_name: dict[str, StorefrontItem] = {}
    for machine_name, item in tier_items.items():
        name = item.get("human_name")
        content_type = item.get("item_content_type")
        if not name or not content_type:
            continue  # charity tip / EFF-style tiles: a human_name but no content_type, not a real product
        price = item.get("min_price|money") or {}
        si = StorefrontItem(
            machine_name=machine_name,
            name=name,
            content_type=content_type,
            msrp_amount=price.get("amount"),
        )
        items.append(si)
        items_by_name[machine_name] = si

    # tier_order is highest-price-first; tier_item_machine_names is cumulative (each tier
    # includes every lower tier's items plus its own new ones) — reverse to low-to-high and
    # diff against everything already seen so each tier only lists what's newly unlocked,
    # matching how Humble's own tier headers read ("...to ALSO unlock!").
    tier_order = list(reversed(bundle_data.get("tier_order") or []))
    tier_display = bundle_data.get("tier_display_data") or {}
    tier_pricing = bundle_data.get("tier_pricing_data") or {}

    tiers: list[StorefrontTier] = []
    seen: set[str] = set()
    for tier_id in tier_order:
        display = tier_display.get(tier_id) or {}
        pricing = tier_pricing.get(tier_id) or {}
        names = list(display.get("tier_item_machine_names") or []) + list(display.get("bonus_item_machine_names") or [])
        new_names = [n for n in names if n not in seen]
        seen.update(names)
        tier_item_list = [items_by_name[n] for n in new_names if n in items_by_name]
        if not tier_item_list:
            continue  # nothing but the charity tile or a repeat unlocked at this price
        tiers.append(
            StorefrontTier(
                identifier=tier_id,
                label=display.get("header") or "",
                price_amount=(pricing.get("price|money") or {}).get("amount"),
                is_bta=bool(pricing.get("is_bta")),
                items=tier_item_list,
            )
        )

    result = StorefrontBundleDetail(
        name=basic_data.get("human_name") or "",
        msrp_total=(basic_data.get("msrp|money") or {}).get("amount"),
        items=items,
        tiers=tiers,
    )
    _detail_cache[product_url] = (time.monotonic(), result)
    return result
