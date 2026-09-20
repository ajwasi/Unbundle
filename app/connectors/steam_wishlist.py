"""Steam wishlist connector.

Two endpoints, because Steam splits the data:

  * IWishlistService/GetWishlist returns appids and nothing else. Confirmed
    callable without an API key (200, not 403); it needs the profile's
    wishlist to be public, the same requirement the owned-games sync already
    documents in Settings.
  * store/api/appdetails supplies name, art and price — one appid at a time.
    Batching appids was tried and returns a flat 400, so a wishlist of N games
    costs N calls. `filters=price_overview` keeps the refresh call small
    (~200 bytes against ~21 kB for the full record), which is why sync only
    pays the full price once per appid, when it first appears.

Nothing here is retried and every call is spaced, because appdetails is rate
limited and this is a manual refresh, not a crawler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
# appdetails is rate limited; this is a manual refresh, so a pause between
# apps costs the user seconds and keeps well clear of the limit.
APP_DELAY_SECONDS = 0.6
# A wishlist longer than this is not a wishlist. The cap bounds the worst
# case of an N-call refresh rather than trusting the input.
MAX_APPS = 500


class SteamWishlistError(Exception):
    """The wishlist could not be read — usually a private profile."""


@dataclass
class SteamWishlistEntry:
    appid: int
    priority: int
    added_at: datetime | None


@dataclass
class SteamAppDetails:
    name: str = ""
    header_image: str = ""
    developers: str = ""
    short_description: str = ""
    current_price: float | None = None
    list_price: float | None = None
    currency: str = ""
    discount_pct: int = 0
    metacritic: int | None = None


def parse_wishlist(payload: dict) -> list[SteamWishlistEntry]:
    """GetWishlist's items. An empty response is a legitimate answer — an
    empty or private wishlist looks identical from here, so it is not treated
    as an error."""
    items = ((payload or {}).get("response") or {}).get("items") or []
    entries: list[SteamWishlistEntry] = []
    for item in items[:MAX_APPS]:
        try:
            appid = int(item.get("appid"))
        except (TypeError, ValueError):
            continue
        added = item.get("date_added")
        entries.append(
            SteamWishlistEntry(
                appid=appid,
                priority=int(item.get("priority") or 0),
                added_at=datetime.utcfromtimestamp(added) if isinstance(added, (int, float)) and added else None,
            )
        )
    return entries


def _price(overview: object) -> tuple[float | None, float | None, str, int]:
    """price_overview reports amounts in minor units (999 == $9.99) and the
    discount as a whole percent."""
    if not isinstance(overview, dict):
        return None, None, "", 0
    try:
        final = float(overview.get("final")) / 100 if overview.get("final") is not None else None
        initial = float(overview.get("initial")) / 100 if overview.get("initial") is not None else None
    except (TypeError, ValueError):
        return None, None, "", 0
    try:
        discount = int(overview.get("discount_percent") or 0)
    except (TypeError, ValueError):
        discount = 0
    return final, initial, str(overview.get("currency") or ""), discount


def _metacritic(node: object) -> int | None:
    """Metacritic, out of 100. Steam's own review percentage is not in the
    appdetails payload, so this is the rating actually on offer here."""
    if not isinstance(node, dict):
        return None
    try:
        score = int(node.get("score"))
    except (TypeError, ValueError):
        return None
    return score if 0 < score <= 100 else None


def parse_app_details(appid: int, payload: dict) -> SteamAppDetails | None:
    """None when Steam reports success=false — a delisted or region-locked
    app, which is ordinary rather than an error."""
    entry = (payload or {}).get(str(appid)) or {}
    if not entry.get("success"):
        return None
    data = entry.get("data") or {}
    final, initial, currency, discount = _price(data.get("price_overview"))
    developers = data.get("developers")
    return SteamAppDetails(
        name=data.get("name") or "",
        header_image=data.get("header_image") or "",
        developers=", ".join(d for d in developers if isinstance(d, str)) if isinstance(developers, list) else "",
        short_description=data.get("short_description") or "",
        current_price=final,
        list_price=initial,
        currency=currency,
        discount_pct=discount,
        metacritic=_metacritic(data.get("metacritic")),
    )


async def fetch_wishlist(steamid: str, timeout: float = 25.0) -> list[SteamWishlistEntry]:
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(WISHLIST_URL, params={"steamid": steamid})
        if resp.status_code >= 400:
            raise SteamWishlistError(
                f"Steam returned {resp.status_code} for the wishlist. The profile's wishlist may be private."
            )
        return parse_wishlist(resp.json())


async def fetch_app_details(
    client: httpx.AsyncClient, appid: int, price_only: bool = False
) -> SteamAppDetails | None:
    params = {"appids": str(appid), "cc": "US", "l": "en"}
    if price_only:
        # ~200 bytes instead of ~21 kB; the fields it omits never change.
        params["filters"] = "price_overview"
    resp = await client.get(APPDETAILS_URL, params=params)
    if resp.status_code >= 400:
        return None
    try:
        return parse_app_details(appid, resp.json())
    except Exception:
        return None
