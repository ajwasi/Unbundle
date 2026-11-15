"""Storefront connector — reads Humble Bundle's currently-for-sale listings
from the public marketing site. This is deliberately separate from
humble_connector.py: it's unauthenticated (no session cookie, no /api/v1),
scrapes embedded JSON off the public HTML instead of calling an API, and
describes bundles for sale rather than bundles already owned.

Both the listing page and individual bundle pages embed their full data. Until
Nov 2026 that was a <script type="application/json"> block; Humble has since
moved to Next.js, which streams the same data inside self.__next_f.push([1, "..."])
calls (React Server Components "flight" data). Both shapes are read: the legacy
script block first, the flight stream as the fallback. Confirmed reachable via a plain GET
with a browser User-Agent — unlike /processlogin, these pages are not behind
Cloudflare's bot check, so no browser automation is needed here.

In-memory TTL cache only. This is public marketing data with no per-user
meaning, so there's nothing worth persisting across restarts, and it goes
stale (bundles rotate) on its own regardless.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import nh3

logger = logging.getLogger(__name__)

BASE_URL = "https://www.humblebundle.com"
# marketing_blurb renders with autoescaping bypassed (see home/_bundles.html) so
# Humble's own <em>-style emphasis shows up as intended — sanitized here rather
# than trusted outright, so a compromised or malicious listing can't inject
# markup/JS through it. Real blurbs confirmed to use at least <em>; the rest of
# this allowlist is the plausible-and-harmless rest of that same family, no
# attributes on any of them (closes off href="javascript:..." / onerror=... etc.
# by construction, not by trying to enumerate bad attributes).
_ALLOWED_BLURB_TAGS = {"em", "strong", "b", "i", "br"}
# An item description is a paragraph or two of real prose, so it needs the
# block tags a blurb doesn't. Still no attributes on any of them, which rules
# out href="javascript:" and onerror= by construction rather than by trying to
# enumerate what's dangerous.
_ALLOWED_DESCRIPTION_TAGS = _ALLOWED_BLURB_TAGS | {"p", "ul", "ol", "li", "h3", "h4", "blockquote", "span"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
LISTING_TTL_SECONDS = 1800
DETAIL_TTL_SECONDS = 1800

_LISTING_ID = "landingPage-json-data"
_DETAIL_ID = "webpack-bundle-page-data"
# Any <script ...>body</script>; attributes are parsed separately so a change in
# attribute order, quoting, or extra attributes (nonce, data-*) can't break the
# match the way a single fixed-string regex did.
_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\b[^>]*>", re.S | re.I)
_ID_RE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.I)


def _find_key(obj: object, key: str, depth: int = 0) -> object | None:
    """First value stored under `key` anywhere in a parsed JSON tree."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = _find_key(child, key, depth + 1)
        if found is not None:
            return found
    return None


def _extract_embedded_json(html: str, script_id: str, shape_key: str) -> object | None:
    """Pull the page's embedded JSON payload out of the HTML.

    Preferred: the <script> whose id is `script_id` (the historical contract).
    Fallback, for when Humble renames the element: any JSON-parsable <script>
    whose tree contains `shape_key` (e.g. "mosaic" for the listing). Returns
    None if neither finds anything.
    """
    candidates: list[tuple[bool, str]] = []
    for attrs, body in _SCRIPT_RE.findall(html):
        id_match = _ID_RE.search(attrs)
        candidates.append((bool(id_match and id_match.group(1) == script_id), body))
    candidates.sort(key=lambda c: not c[0])  # exact-id match first

    for is_exact, body in candidates:
        body = body.strip()
        if not body or body[0] not in "{[":
            continue
        try:
            parsed = json.loads(body)
        except ValueError:
            continue
        if is_exact:
            return parsed
        if _find_key(parsed, shape_key) is not None:
            logger.warning("Storefront: %s not found by id; using a script matched by shape (%s)", script_id, shape_key)
            return parsed
    return None


def _describe_page(resp: httpx.Response) -> str:
    """Diagnostics for a parse failure: what did we actually get back?"""
    ids = [m.group(1) for attrs, _ in _SCRIPT_RE.findall(resp.text) if (m := _ID_RE.search(attrs))]
    title = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.S | re.I)
    return (
        f"status={resp.status_code} final_url={resp.url} bytes={len(resp.content)} "
        f"title={(title.group(1).strip()[:80] if title else None)!r} script_ids={ids[:15]}"
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
    # Everything below comes from the same tier_item_data entry the fields
    # above already read — no extra request. Confirmed present on all 95 items
    # across three live bundles (2026-09-19). Defaulted so every existing
    # construction site and test keeps working unchanged.
    description: str = ""  # sanitized HTML, see _ALLOWED_DESCRIPTION_TAGS
    authors: list[str] = field(default_factory=list)  # Humble calls these "developers"
    publishers: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)  # e.g. ["PDF", "EPUB"]
    delivery_methods: list[str] = field(default_factory=list)  # e.g. ["DRM-free download", "Steam"]
    image_url: str = ""
    image_url_2x: str = ""  # retina variant, for srcset


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


# --- Next.js flight-stream reading (Humble's site since Nov 2026) ---------------

_FLIGHT_MARKER = "self.__next_f.push("
_json = json.JSONDecoder()

_FLIGHT_LABELS = {"pdf": "PDF", "epub": "EPUB", "mobi": "MOBI", "cbz": "CBZ", "mp3": "MP3", "flac": "FLAC"}
_FLIGHT_DELIVERY = {"download": "DRM-free download", "steam": "Steam", "gog": "GOG", "itch": "itch.io"}


def _flight_text(html: str) -> str:
    """Every self.__next_f.push([1, "<chunk>"]) payload, concatenated."""
    chunks: list[str] = []
    pos = 0
    while (i := html.find(_FLIGHT_MARKER, pos)) != -1:
        start = i + len(_FLIGHT_MARKER)
        try:
            arr, end = _json.raw_decode(html, start)
        except ValueError:
            pos = start
            continue
        pos = end
        if isinstance(arr, list) and len(arr) == 2 and arr[0] == 1 and isinstance(arr[1], str):
            chunks.append(arr[1])
    return "".join(chunks)


def _objects_after(text: str, marker: str):
    """Yield the JSON value that immediately follows each occurrence of `marker`."""
    pos = 0
    while (i := text.find(marker, pos)) != -1:
        start = i + len(marker)
        pos = start
        try:
            value, _ = _json.raw_decode(text, start)
        except ValueError:
            continue
        yield value


def _real(value: object) -> object | None:
    """Flight data spells "undefined" as the string "$undefined" and uses other
    "$<hex>" strings as references to rows we don't resolve — neither is a value."""
    if isinstance(value, str) and value.startswith("$"):
        return None
    return value


def _flight_listing(html: str) -> list[StorefrontBundle]:
    seen: set[str] = set()
    bundles: list[StorefrontBundle] = []
    for hit in _objects_after(_flight_text(html), '{"relationTo":"algolia-hit","value":'):
        if not isinstance(hit, dict) or hit.get("productType") != "humble-bundle":
            continue
        url = hit.get("url")
        if not isinstance(url, str) or not url.startswith("/") or url in seen:
            continue
        seen.add(url)
        titles = [t for t in (hit.get("includedItemTitles") or []) if isinstance(t, str)]
        blurb = ""
        if titles:
            more = len(titles) > 3 or (hit.get("itemsCount") or 0) > 3
            blurb = f"Includes {', '.join(titles[:3])}{' and more' if more else ''}"
        bundles.append(
            StorefrontBundle(
                category=hit.get("category") or "",
                machine_name=hit.get("productKey") or "",
                name=hit.get("title") or "",
                blurb=nh3.clean(blurb, tags=_ALLOWED_BLURB_TAGS, attributes={}),
                product_url=f"{BASE_URL}{url}",
                image_url=hit.get("thumbnail") or "",
                end_date=_parse_datetime(_real(hit.get("endDate"))),
            )
        )
    return bundles


def _bundle_key(page: dict) -> str:
    """The bundle's machine_name, recovered from its tiers' skus
    ("<bundle key>_initial", "<bundle key>_bt18", ...) — the page has no field
    for it, but the listing's productKey and an order's product.machine_name
    are this same value."""
    for tier in page.get("tiers") or []:
        sku = tier.get("sku") if isinstance(tier, dict) else None
        if isinstance(sku, str) and "_" in sku:
            return sku.rsplit("_", 1)[0]
    return ""


def _flight_item(item: dict, fallback_type: str, bundle_key: str = "") -> StorefrontItem | None:
    key, title = item.get("key"), item.get("title")
    if not key or not title:
        return None
    # Flight data keys an item "<item machine_name>_<bundle key>"; owned orders
    # (and so the catalog every ownership check compares against) carry the bare
    # item machine_name. Left suffixed, nothing ever matched and every item
    # read as not owned.
    suffix = f"_{bundle_key}" if bundle_key else ""
    if suffix and key.endswith(suffix) and len(key) > len(suffix):
        key = key[: -len(suffix)]
    msrp = item.get("msrp")
    description = _real(item.get("description"))
    platforms = [p for p in (item.get("platforms") or []) if isinstance(p, str)]
    delivery = [d for d in (item.get("deliveryMethods") or []) if isinstance(d, str)]
    return StorefrontItem(
        machine_name=key,
        name=title,
        content_type=item.get("bundleType") or fallback_type or None,
        msrp_amount=msrp.get("amount") if isinstance(msrp, dict) else None,
        description=(
            nh3.clean(description, tags=_ALLOWED_DESCRIPTION_TAGS, attributes={}) if isinstance(description, str) else ""
        ),
        authors=_named(item.get("developers"), "name"),
        publishers=_named(item.get("publishers"), "name"),
        formats=[_FLIGHT_LABELS.get(p, p.title()) for p in platforms],
        delivery_methods=[_FLIGHT_DELIVERY.get(d, d.title()) for d in delivery],
        image_url=item.get("thumbnailImageUrl") or "",
    )


def _flight_detail(html: str) -> StorefrontBundleDetail | None:
    page = next(
        (
            p
            for p in _objects_after(_flight_text(html), '"bundlePage":')
            if isinstance(p, dict) and isinstance(p.get("tiers"), list)
        ),
        None,
    )
    if page is None:
        return None
    page_type = page.get("bundleType") or ""
    bundle_key = _bundle_key(page)

    def _price(tier: dict) -> float | None:
        price = tier.get("price")
        return price.get("amount") if isinstance(price, dict) else None

    items: list[StorefrontItem] = []
    seen: set[str] = set()
    tiers: list[StorefrontTier] = []
    # A higher tier's bundleItems repeats lower tiers' items as string references
    # ("$e:0:props:..."); only the dict entries are real items, and anything
    # already seen is skipped, so each tier lists just what it newly unlocks.
    ordered = sorted((t for t in page["tiers"] if isinstance(t, dict)), key=lambda t: _price(t) or 0)
    for index, tier in enumerate(ordered):
        new_items = []
        for raw in tier.get("bundleItems") or []:
            si = _flight_item(raw, page_type, bundle_key) if isinstance(raw, dict) else None
            if si is None or si.machine_name in seen:
                continue
            seen.add(si.machine_name)
            new_items.append(si)
        items.extend(new_items)
        if not new_items:
            continue
        price = _price(tier)
        amount = f"${price:g}" if price is not None else "this tier"
        tiers.append(
            StorefrontTier(
                identifier=str(tier.get("id") or index + 1),
                label=f"Pay {amount} or more{' to also unlock!' if tiers else ' to unlock'}",
                price_amount=price,
                is_bta=False,
                items=new_items,
            )
        )

    value = page.get("value")
    return StorefrontBundleDetail(
        name=page.get("title") or "",
        msrp_total=value.get("amount") if isinstance(value, dict) else None,
        items=items,
        tiers=tiers,
    )


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
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # The flight data carries explicit offsets ("...Z"); the rest of the app
    # (and the "Z" the countdown template appends) expects naive UTC.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _legacy_listing(payload: object, resp: httpx.Response) -> list[StorefrontBundle]:
    data = (payload.get("data") if isinstance(payload, dict) else None) or {}
    if not isinstance(data, dict) or not any(isinstance(v, dict) and "mosaic" in v for v in data.values()):
        # Shape moved (e.g. wrapped one level deeper) — look for the category map itself.
        inner = _find_key(payload, "data")
        data = inner if isinstance(inner, dict) else {}

    bundles: list[StorefrontBundle] = []
    if not data:
        raise ValueError(f"Found embedded JSON on the bundles listing page but no category data ({_describe_page(resp)})")
    for category, section in data.items():
        if not isinstance(section, dict):
            continue
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
    return bundles


async def fetch_current_bundles(force: bool = False) -> list[StorefrontBundle]:
    global _listing_cache, _listing_fetched_at
    if _listing_cache and not force and time.monotonic() - _listing_cache[0] < LISTING_TTL_SECONDS:
        return _listing_cache[1]

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(f"{BASE_URL}/bundles")
        resp.raise_for_status()

    payload = _extract_embedded_json(resp.text, _LISTING_ID, "mosaic")
    if payload is None:
        bundles = _flight_listing(resp.text)
        if not bundles:
            raise ValueError(
                f"Could not find {_LISTING_ID} or Next.js bundle data on the bundles listing page ({_describe_page(resp)})"
            )
    else:
        bundles = _legacy_listing(payload, resp)

    _listing_cache = (time.monotonic(), bundles)
    _listing_fetched_at = datetime.utcnow()
    return bundles


def _named(entries: object, key: str) -> list[str]:
    """Humble wraps each name in its own single-key dict, e.g.
    developers: [{"developer-name": "Stan Sakai"}]."""
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if isinstance(entry, dict):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                out.append(value.strip())
    return out


# Which resolved_paths key holds an item's art, most preferred first, paired
# with its retina variant where one exists.
#
# There is no single key every item uses, and the split runs along bundle
# type: book and comic items carry front_page_art_imgix, while game items
# frequently have that null and populate featured_image instead. An earlier
# version read only front_page_art_imgix (plus preview_image) because it was
# checked against three book bundles — which is exactly how a narrow sample
# produces a rule that looks universal and silently drops half a games
# bundle's covers.
_IMAGE_KEYS = (
    ("front_page_art_imgix", "front_page_art_imgix_retina"),
    ("featured_image", ""),
    ("preview_image", "preview_image_bonus_retina"),
)


def _item_images(resolved: dict) -> dict:
    """First populated art key wins, and the 2x only comes from that same
    entry — pairing a retina URL with a different image's 1x would serve two
    unrelated pictures depending on screen density."""
    for key, retina_key in _IMAGE_KEYS:
        url = resolved.get(key)
        if isinstance(url, str) and url:
            retina = resolved.get(retina_key) if retina_key else ""
            return {"image_url": url, "image_url_2x": retina if isinstance(retina, str) else ""}
    return {"image_url": "", "image_url_2x": ""}


def _availability(item: dict) -> tuple[list[str], list[str]]:
    """Split availability_icons into (formats, delivery methods).

    `human_names` maps icon keys to labels for both kinds at once —
    {"hb-file-pdf-o": "PDF", "hb-drmfree": "DRM-free download"} — and
    `delivery_to_platform` is what distinguishes them: its keys are the
    delivery methods, and the `available` lists under them are the formats
    each one ships. Reading the split from the data beats guessing which
    labels look like a file type.
    """
    icons = item.get("availability_icons") or {}
    human = icons.get("human_names") or {}
    mapping = icons.get("delivery_to_platform") or {}
    if not isinstance(human, dict) or not isinstance(mapping, dict):
        return [], []

    delivery, formats = [], []
    for delivery_key, platforms in mapping.items():
        label = human.get(delivery_key)
        if isinstance(label, str) and label and label not in delivery:
            delivery.append(label)
        available = (platforms or {}).get("available") if isinstance(platforms, dict) else None
        for format_key in available or []:
            format_label = human.get(format_key)
            if isinstance(format_label, str) and format_label and format_label not in formats:
                formats.append(format_label)
    return formats, delivery


def _legacy_detail(payload: object) -> StorefrontBundleDetail:
    bundle_data = (_find_key(payload, "bundleData")) or {}
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
        formats, delivery = _availability(item)
        resolved = item.get("resolved_paths") or {}
        si = StorefrontItem(
            machine_name=machine_name,
            name=name,
            content_type=content_type,
            msrp_amount=price.get("amount"),
            description=nh3.clean(item.get("description_text") or "", tags=_ALLOWED_DESCRIPTION_TAGS, attributes={}),
            authors=_named(item.get("developers"), "developer-name"),
            publishers=_named(item.get("publishers"), "publisher-name"),
            formats=formats,
            delivery_methods=delivery,
            **_item_images(resolved),
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

    return StorefrontBundleDetail(
        name=basic_data.get("human_name") or "",
        msrp_total=(basic_data.get("msrp|money") or {}).get("amount"),
        items=items,
        tiers=tiers,
    )


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

    payload = _extract_embedded_json(resp.text, _DETAIL_ID, "bundleData")
    if payload is not None:
        result = _legacy_detail(payload)
    else:
        result = _flight_detail(resp.text)
        if result is None:
            raise ValueError(f"Could not find {_DETAIL_ID} or Next.js bundle data on {product_url} ({_describe_page(resp)})")
    _detail_cache[product_url] = (time.monotonic(), result)
    return result
