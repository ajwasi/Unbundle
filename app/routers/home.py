"""Home page: shows Humble Bundle's currently-for-sale bundles (games, books,
software) pulled live from the public storefront (see connectors/storefront.py),
and a compare view that cross-references a for-sale bundle's contents against
the user's own Catalog via the shared machine_name identity key.
"""

import asyncio
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import storefront
from app.csrf import require_csrf
from app.deps import get_db
from app.models.bundle import Bundle
from app.models.credential import SOURCE_HUMBLE, Credential
from app.ratelimit import RateLimiter, rate_limit
from app.routers.catalog import _avg_item_value, _build_catalog
from app.templates_env import templates

router = APIRouter()

CATEGORY_LABELS = [("games", "Games"), ("books", "Books"), ("software", "Software")]
_ALLOWED_STOREFRONT_HOST = urlparse(storefront.BASE_URL).hostname
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)


def _grouped(tiles: list[dict]) -> dict[str, list]:
    by_category: dict[str, list] = {key: [] for key, _ in CATEGORY_LABELS}
    for tile in tiles:
        by_category.setdefault(tile["bundle"].category, []).append(tile)
    for group in by_category.values():
        group.sort(key=lambda tile: tile["bundle"].start_date or datetime.min, reverse=True)
    return by_category


async def _fetch_detail_safe(bundle: storefront.StorefrontBundle) -> storefront.StorefrontBundleDetail | None:
    """None on any failure — a single slow/broken bundle detail page degrades
    that one tile to "no ratio shown," never a broken Home page."""
    try:
        return await storefront.fetch_bundle_detail(bundle.product_url)
    except Exception:
        return None


async def _build_tiles(bundles: list[storefront.StorefrontBundle], db: Session) -> list[dict]:
    """One dict per currently-listed bundle: the bundle itself, the gamekey of
    the already-purchased order for this *exact* bundle if any (by
    machine_name — see Bundle's own docstring), and how many of its items are
    already owned via *any* purchase (owned_count/total_count, both None when
    that bundle's detail fetch failed — distinct from a real 0/0).

    Every bundle's detail is fetched concurrently rather than one at a time —
    fetch_bundle_detail() already caches per-URL for 30 min, so only a cold
    cache pays the full fan-out cost.
    """
    details = await asyncio.gather(*(_fetch_detail_safe(b) for b in bundles))
    catalog = _build_catalog(db)
    purchased_gamekeys_by_machine_name = {
        name: gamekey
        for name, gamekey in db.query(Bundle.machine_name, Bundle.gamekey).filter(Bundle.machine_name != "").all()
    }

    tiles = []
    for bundle, detail in zip(bundles, details):
        owned_count = total_count = None
        if detail is not None:
            total_count = len(detail.items)
            owned_count = sum(1 for item in detail.items if item.machine_name in catalog)
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


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    bundles = await storefront.fetch_current_bundles()
    tiles = await _build_tiles(bundles, db)
    return templates.TemplateResponse(
        request,
        "home/index.html",
        {
            "by_category": _grouped(tiles),
            "category_labels": CATEGORY_LABELS,
            "last_synced": storefront.last_fetched_at(),
            # Absence of the row, not its status — a saved-but-failing key means
            # the user has already been through setup and gets the error on the
            # Settings card instead of a getting-started banner here.
            "humble_configured": Credential.get(db, SOURCE_HUMBLE) is not None,
        },
    )


@router.post("/storefront/refresh", response_class=HTMLResponse, dependencies=[Depends(rate_limit(_refresh_limiter, "storefront-refresh")), Depends(require_csrf)])
async def refresh_storefront(request: Request, db: Session = Depends(get_db)):
    bundles = await storefront.fetch_current_bundles(force=True)
    tiles = await _build_tiles(bundles, db)
    return templates.TemplateResponse(
        request,
        "home/_bundles.html",
        {
            "by_category": _grouped(tiles),
            "category_labels": CATEGORY_LABELS,
            "last_synced": storefront.last_fetched_at(),
        },
    )


@router.get("/storefront/compare", response_class=HTMLResponse)
async def compare(request: Request, url: str, view: str = "list", db: Session = Depends(get_db)):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != _ALLOWED_STOREFRONT_HOST:
        raise HTTPException(status_code=400, detail="Invalid bundle URL")

    detail = await storefront.fetch_bundle_detail(url)
    catalog = _build_catalog(db)

    def _row(item: storefront.StorefrontItem) -> dict:
        owned_entry = catalog.get(item.machine_name)
        avg_value = _avg_item_value(owned_entry["bundles"]) if owned_entry else None
        return {
            "machine_name": item.machine_name,
            "name": item.name,
            "content_type": item.content_type,
            "msrp_amount": item.msrp_amount,
            "avg_paid": avg_value,
            "owned": bool(owned_entry),
            "owned_count": len(owned_entry["bundles"]) if owned_entry else 0,
            # Already parsed out of the same page fetch — see StorefrontItem.
            "description": item.description,
            "authors": item.authors,
            "publishers": item.publishers,
            "formats": item.formats,
            "delivery_methods": item.delivery_methods,
            "image_url": item.image_url,
            "image_url_2x": item.image_url_2x,
        }

    tiers = []
    for tier in detail.tiers:
        tier_rows = sorted((_row(i) for i in tier.items), key=lambda r: (not r["owned"], r["name"].lower()))
        tiers.append(
            {
                "label": tier.label,
                "price_amount": tier.price_amount,
                "is_bta": tier.is_bta,
                "rows": tier_rows,
                "owned_count": sum(1 for r in tier_rows if r["owned"]),
            }
        )

    owned_count = sum(1 for t in tiers for r in t["rows"] if r["owned"])
    total_count = sum(len(t["rows"]) for t in tiers)

    return templates.TemplateResponse(
        request,
        "home/compare.html",
        {
            "bundle_name": detail.name,
            "msrp_total": detail.msrp_total,
            "product_url": url,
            "tiers": tiers,
            "owned_count": owned_count,
            "total_count": total_count,
            # Grid mirrors Humble's own page; list is the original dense view.
            # A query parameter rather than a stored preference so a link to
            # either keeps the view it was shared with.
            "view": "grid" if view == "grid" else "list",
        },
    )
