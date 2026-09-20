"""Steam and GOG wishlist refreshes.

Both are manual-only, like every other refresh in this app.

The two differ sharply in cost and the code reflects that rather than
pretending otherwise: GOG prices its whole wishlist in one batch call, while
Steam has no batch endpoint at all and pays one call per app. Steam therefore
caches everything that never changes (name, art, developer) and asks only for
price on later refreshes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.connectors import gog_connector, gog_wishlist, steam_wishlist
from app.models.credential import SOURCE_GOG, SOURCE_STEAM, Credential
from app.models.store_wishlist import (
    GogWishlistItem,
    GogWishlistPrice,
    SteamWishlistItem,
    SteamWishlistPrice,
)
from app.sync.wishlist_common import flag_removed, record_price_if_changed


class NotConnectedError(Exception):
    """The store this wishlist belongs to has no stored credential."""


# ------------------------------------------------------------------- steam


async def refresh_steam_wishlist(db: Session) -> dict:
    payload = Credential.get_payload(db, SOURCE_STEAM)
    if not payload or not payload.get("steamid64"):
        raise NotConnectedError("Steam is not connected yet — connect it in Settings.")

    entries = await steam_wishlist.fetch_wishlist(payload["steamid64"])

    now = datetime.utcnow()
    seen: set[int] = set()
    new = price_changes = detail_fetches = 0

    async with httpx.AsyncClient(
        timeout=25.0, headers={"User-Agent": steam_wishlist.USER_AGENT}
    ) as client:
        for index, entry in enumerate(entries):
            seen.add(entry.appid)
            row = db.get(SteamWishlistItem, entry.appid)
            if row is None:
                row = SteamWishlistItem(appid=entry.appid, first_seen_at=now)
                db.add(row)
                new += 1

            # Full record only the first time an appid is seen; after that the
            # small price-only response is enough, which is what makes an
            # N-call refresh tolerable.
            price_only = row.details_fetched_at is not None and bool(row.name)
            details = await steam_wishlist.fetch_app_details(client, entry.appid, price_only=price_only)
            if index < len(entries) - 1:
                await asyncio.sleep(steam_wishlist.APP_DELAY_SECONDS)

            if details is not None:
                if not price_only:
                    row.name = details.name or row.name
                    row.header_image = details.header_image or row.header_image
                    row.developers = details.developers or row.developers
                    row.short_description = details.short_description or row.short_description
                    detail_fetches += 1
                row.current_price = details.current_price
                row.list_price = details.list_price
                row.currency = details.currency
                row.discount_pct = details.discount_pct
                row.details_fetched_at = now

            row.priority = entry.priority
            row.added_at = entry.added_at
            row.last_seen_at = now
            row.removed_at = None
            db.flush()
            if record_price_if_changed(
                db, SteamWishlistPrice, "appid", row.appid, row.current_price, row.list_price, row.currency, now
            ):
                price_changes += 1

    removed = flag_removed(db, SteamWishlistItem, "appid", seen, now)
    db.commit()
    return {
        "total": len(entries),
        "new": new,
        "price_changes": price_changes,
        "detail_fetches": detail_fetches,
        "removed": removed,
    }


# --------------------------------------------------------------------- gog


async def refresh_gog_wishlist(db: Session) -> dict:
    payload = Credential.get_payload(db, SOURCE_GOG)
    if not payload or not payload.get("refresh_token"):
        raise NotConnectedError("GOG is not connected yet — connect it in Settings.")

    tokens = await gog_connector.refresh_access_token(payload["refresh_token"])
    product_ids = await gog_wishlist.fetch_wishlist_ids(tokens["access_token"])
    prices = {p.product_id: p for p in await gog_wishlist.fetch_prices(product_ids)}

    now = datetime.utcnow()
    new = price_changes = detail_fetches = 0

    async with httpx.AsyncClient(
        timeout=25.0, headers={"User-Agent": gog_wishlist.USER_AGENT}
    ) as client:
        for product_id in product_ids:
            row = db.get(GogWishlistItem, product_id)
            if row is None:
                row = GogWishlistItem(product_id=product_id, first_seen_at=now)
                db.add(row)
                new += 1

            # Title and art never change, so only a product never seen before
            # costs a lookup.
            if not row.title:
                details = await gog_wishlist.fetch_product(client, product_id)
                if details:
                    row.title, row.cover_url, row.store_url = details
                    detail_fetches += 1

            price = prices.get(product_id)
            if price is not None:
                row.current_price = price.current_price
                row.list_price = price.list_price
                row.currency = price.currency
            row.details_fetched_at = now
            row.last_seen_at = now
            row.removed_at = None
            db.flush()
            if record_price_if_changed(
                db,
                GogWishlistPrice,
                "product_id",
                row.product_id,
                row.current_price,
                row.list_price,
                row.currency,
                now,
            ):
                price_changes += 1

    removed = flag_removed(db, GogWishlistItem, "product_id", set(product_ids), now)
    db.commit()
    return {
        "total": len(product_ids),
        "new": new,
        "price_changes": price_changes,
        "detail_fetches": detail_fetches,
        "removed": removed,
    }
