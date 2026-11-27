"""Storefront bundle tiles: the "currently for sale" bundles joined with what
the user already owns. Shared by the Home page and the Deals page (which
shows the same tiles), so it lives here rather than in either router.
"""

import asyncio
from datetime import datetime

from sqlalchemy.orm import Session

from app.connectors import storefront
from app.models.bundle import Bundle
from app.services.catalog import build_catalog
from app.services.owned_elsewhere import owned_via_title, storefront_library_titles


CATEGORY_LABELS = [("games", "Games"), ("books", "Books"), ("software", "Software")]


def grouped(tiles: list[dict]) -> dict[str, list]:
    by_category: dict[str, list] = {key: [] for key, _ in CATEGORY_LABELS}
    for tile in tiles:
        by_category.setdefault(tile["bundle"].category, []).append(tile)
    for group in by_category.values():
        group.sort(key=lambda tile: tile["bundle"].start_date or datetime.min, reverse=True)
    return by_category


async def fetch_detail_safe(bundle: storefront.StorefrontBundle) -> storefront.StorefrontBundleDetail | None:
    """None on any failure — a single slow/broken bundle detail page degrades
    that one tile to "no ratio shown," never a broken Home page."""
    try:
        return await storefront.fetch_bundle_detail(bundle.product_url)
    except Exception:  # noqa: BLE001 - one broken bundle detail degrades that tile only
        return None


async def build_tiles(bundles: list[storefront.StorefrontBundle], db: Session) -> list[dict]:
    """One dict per currently-listed bundle: the bundle itself, the gamekey of
    the already-purchased order for this *exact* bundle if any (by
    machine_name — see Bundle's own docstring), and how many of its items are
    already owned via *any* purchase (owned_count/total_count, both None when
    that bundle's detail fetch failed — distinct from a real 0/0).

    Every bundle's detail is fetched concurrently rather than one at a time —
    fetch_bundle_detail() already caches per-URL for 30 min, so only a cold
    cache pays the full fan-out cost.
    """
    details = await asyncio.gather(*(fetch_detail_safe(b) for b in bundles))
    catalog = build_catalog(db)
    libraries = storefront_library_titles(db)
    purchased_gamekeys_by_machine_name = {
        name: gamekey
        for name, gamekey in db.query(Bundle.machine_name, Bundle.gamekey).filter(Bundle.machine_name != "").all()
    }

    tiles = []
    for bundle, detail in zip(bundles, details):
        owned_count = total_count = None
        if detail is not None:
            total_count = len(detail.items)
            owned_count = sum(
                1 for item in detail.items if item.machine_name in catalog or owned_via_title(libraries, item.name)
            )
        owned_gamekey = purchased_gamekeys_by_machine_name.get(bundle.machine_name)
        tiles.append(
            {
                "bundle": bundle,
                "already_purchased": owned_gamekey is not None,
                "owned_gamekey": owned_gamekey,
                "owned_count": owned_count,
                "total_count": total_count,
            }
        )
    return tiles
