"""GOG discounted-catalogue connector.

Deliberately separate from gog_connector.py: that one is authenticated
(OAuth against embed.gog.com, describing what you already own), this one is
unauthenticated public marketing data from catalog.gog.com describing what is
on sale. No credential is needed or used.

In-memory TTL cache only, the same reasoning storefront.py applies to Humble's
listings: discounts rotate on their own, there is no per-user meaning to
persist, and a stale row would be worse than no row.

The one thing that makes this page worth opening is that GOG's product `id`
is the same identifier as GogGame.product_id — so "do I already own this?"
is an exact join, not the title matching gog_sync.py explicitly refuses to do.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from app.config import settings

CATALOG_URL = "https://catalog.gog.com/v1/catalog"
TTL_SECONDS = 1800
PAGE_LIMIT = 48
# A few pages of the biggest discounts is the useful part; GOG reports
# thousands of discounted products and nobody scrolls that far.
MAX_PAGES = 4

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


@dataclass
class GogDeal:
    product_id: int
    title: str
    slug: str
    store_url: str
    cover_url: str
    price_final: float | None
    price_base: float | None
    currency: str
    discount_pct: int | None
    operating_systems: list[str] = field(default_factory=list)
    developers: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    rating: float | None = None


_cache: tuple[float, list[GogDeal], int] | None = None


def last_fetched_age() -> float | None:
    """Seconds since the cache was filled, or None if it never has been."""
    return (time.monotonic() - _cache[0]) if _cache else None


def _money(node: object) -> float | None:
    """GOG gives both a display string ("$1.49") and a numeric
    {"amount": "1.49"}. Only the numeric one is parsed — the display string
    carries a currency symbol this app would have to guess at."""
    if not isinstance(node, dict):
        return None
    try:
        return float(node.get("amount"))
    except (TypeError, ValueError):
        return None


def _discount_pct(price: dict) -> int | None:
    """GOG reports discount as "-95%". Parsed rather than recomputed from the
    two amounts so the number shown is the one GOG itself advertises."""
    raw = price.get("discount")
    if not isinstance(raw, str):
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def _rating(raw: object) -> float | None:
    """reviewsRating is a 0-50 integer, i.e. a five-star score in tenths —
    inferred from 192 live products spanning 0 to 49, never above. Returned
    out of 5 so the template doesn't have to know that.

    Zero means unrated rather than a genuine nought: it was the single most
    common value in that sample, which no real rating distribution looks
    like. Showing it as "0.0 stars" would libel a lot of games.
    """
    if not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return round(float(raw) / 10, 1)


def _names(entries: object) -> list[str]:
    """developers/publishers are plain strings; genres are {"name": …}."""
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            out.append(entry.strip())
        elif isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return out


def parse_deal(product: dict) -> GogDeal | None:
    """None for a product with no usable identity — it could not be matched
    against the owned library or linked to, so it has no place on the page."""
    raw_id = product.get("id")
    try:
        product_id = int(raw_id)
    except (TypeError, ValueError):
        return None

    price = product.get("price") if isinstance(product.get("price"), dict) else {}
    rating = _rating(product.get("reviewsRating"))
    return GogDeal(
        product_id=product_id,
        title=product.get("title") or "",
        slug=product.get("slug") or "",
        store_url=product.get("storeLink") or "",
        cover_url=product.get("coverVertical") or product.get("coverHorizontal") or "",
        price_final=_money(price.get("finalMoney")),
        price_base=_money(price.get("baseMoney")),
        currency=(price.get("finalMoney") or {}).get("currency") or "",
        discount_pct=_discount_pct(price),
        operating_systems=_names(product.get("operatingSystems")),
        developers=_names(product.get("developers")),
        genres=_names(product.get("genres"))[:3],
        rating=rating,
    )


async def fetch_deals(force: bool = False) -> tuple[list[GogDeal], int]:
    """(deals, total discounted products GOG reports). Cached for TTL_SECONDS."""
    global _cache
    if _cache and not force and time.monotonic() - _cache[0] < TTL_SECONDS:
        return _cache[1], _cache[2]

    deals: list[GogDeal] = []
    total = 0
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        for page in range(1, MAX_PAGES + 1):
            resp = await client.get(
                CATALOG_URL,
                params={
                    "limit": PAGE_LIMIT,
                    "page": page,
                    "order": "desc:discount",
                    "discounted": "eq:true",
                    "productType": "in:game,pack",
                    "countryCode": settings.gog_country,
                    "currencyCode": settings.gog_currency,
                    "locale": "en-US",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            total = data.get("productCount") or total
            products = data.get("products") or []
            for product in products:
                parsed = parse_deal(product)
                if parsed:
                    deals.append(parsed)
            if len(products) < PAGE_LIMIT:
                break

    _cache = (time.monotonic(), deals, total)
    return deals, total
