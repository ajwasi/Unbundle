"""GOG wishlist connector.

Cheap compared with Steam's, because GOG batches:

  * embed.gog.com/user/wishlist.json needs the stored OAuth access token
    (it returns 403 with a JSON body unauthenticated, confirmed) and gives
    back the wishlisted product ids.
  * api.gog.com/products/prices?ids=1,2,3 prices the whole list in ONE public
    call — three products came back in 926 bytes — so wishlist size does not
    drive request count.
  * api.gog.com/products/{id} fills in title and art, and is only called for
    a product not seen before, since neither changes.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings

WISHLIST_URL = "https://embed.gog.com/user/wishlist.json"
PRICES_URL = "https://api.gog.com/products/prices"
PRODUCT_URL = "https://api.gog.com/products/{product_id}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
# The prices endpoint takes a comma-joined list; chunked so one enormous
# wishlist cannot produce a URL long enough to be rejected.
PRICE_CHUNK = 50
MAX_PRODUCTS = 1000


class GogWishlistError(Exception):
    """The wishlist could not be read — usually an expired login."""


@dataclass
class GogWishlistPriceData:
    product_id: int
    current_price: float | None
    list_price: float | None
    currency: str


def parse_wishlist(payload: dict) -> list[int]:
    """GOG returns {"wishlist": {"<product id>": true, ...}}.

    Values are a flag rather than data, so only the keys matter; a false one
    means "not wishlisted" and is skipped.
    """
    wishlist = (payload or {}).get("wishlist")
    if not isinstance(wishlist, dict):
        return []
    ids: list[int] = []
    for key, flag in wishlist.items():
        if not flag:
            continue
        try:
            ids.append(int(key))
        except (TypeError, ValueError):
            continue
    return ids[:MAX_PRODUCTS]


def _amount(raw: object) -> float | None:
    """GOG writes money as "2959 USD" — minor units and a currency in one
    string."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    head = raw.split()[0]
    try:
        return int(head) / 100
    except ValueError:
        return None


def _currency(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    parts = raw.split()
    return parts[1] if len(parts) > 1 else ""


def parse_prices(payload: dict) -> list[GogWishlistPriceData]:
    items = ((payload or {}).get("_embedded") or {}).get("items") or []
    out: list[GogWishlistPriceData] = []
    for item in items:
        embedded = (item or {}).get("_embedded") or {}
        product = embedded.get("product") or {}
        try:
            product_id = int(product.get("id"))
        except (TypeError, ValueError):
            continue
        prices = embedded.get("prices") or []
        if not prices:
            out.append(GogWishlistPriceData(product_id, None, None, ""))
            continue
        first = prices[0] or {}
        out.append(
            GogWishlistPriceData(
                product_id=product_id,
                current_price=_amount(first.get("finalPrice")),
                list_price=_amount(first.get("basePrice")),
                currency=_currency(first.get("finalPrice")),
            )
        )
    return out


def parse_product(payload: dict) -> tuple[str, str, str]:
    """(title, cover url, store url) for one product."""
    images = (payload or {}).get("images") or {}
    cover = images.get("logo2x") or images.get("logo") or images.get("background") or ""
    if isinstance(cover, str) and cover.startswith("//"):
        cover = f"https:{cover}"
    links = (payload or {}).get("links") or {}
    return (payload or {}).get("title") or "", cover or "", links.get("product_card") or ""


async def fetch_wishlist_ids(access_token: str, timeout: float = 25.0) -> list[int]:
    headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        resp = await client.get(WISHLIST_URL)
        if resp.status_code in (401, 403):
            raise GogWishlistError("GOG rejected the stored login. Reconnect GOG in Settings.")
        if resp.status_code >= 400:
            raise GogWishlistError(f"GOG returned {resp.status_code} for the wishlist.")
        return parse_wishlist(resp.json())


async def fetch_prices(product_ids: list[int], timeout: float = 25.0) -> list[GogWishlistPriceData]:
    """One call per chunk, not per product — the whole point of this endpoint."""
    out: list[GogWishlistPriceData] = []
    if not product_ids:
        return out
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        for start in range(0, len(product_ids), PRICE_CHUNK):
            chunk = product_ids[start : start + PRICE_CHUNK]
            resp = await client.get(
                PRICES_URL,
                params={"ids": ",".join(str(i) for i in chunk), "countryCode": settings.gog_country},
            )
            if resp.status_code >= 400:
                continue  # a bad chunk costs prices, never the whole sync
            out.extend(parse_prices(resp.json()))
    return out


async def fetch_product(client: httpx.AsyncClient, product_id: int) -> tuple[str, str, str] | None:
    resp = await client.get(PRODUCT_URL.format(product_id=product_id))
    if resp.status_code >= 400:
        return None
    try:
        return parse_product(resp.json())
    except Exception:
        return None
