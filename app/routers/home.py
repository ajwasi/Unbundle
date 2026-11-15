"""Home page: shows Humble Bundle's currently-for-sale bundles (games, books,
software) pulled live from the public storefront (see connectors/storefront.py),
and a compare view that cross-references a for-sale bundle's contents against
the user's own Catalog via the shared machine_name identity key.
"""

import logging
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import storefront
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_HUMBLE, Credential
from app.ratelimit import RateLimiter, rate_limit
from app.services import source_health
from app.services.bundle_tiles import CATEGORY_LABELS, build_tiles, grouped
from app.services.catalog import avg_item_value, build_catalog
from app.templates_env import templates

router = APIRouter()
logger = logging.getLogger(__name__)

_ALLOWED_STOREFRONT_HOST = urlparse(storefront.BASE_URL).hostname
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)


async def _fetch_listing_safe(
    db: Session, force: bool = False
) -> tuple[list[storefront.StorefrontBundle], str | None, datetime | None]:
    """(bundles, error, last_ok). Humble's markup isn't a contract (it moved to
    Next.js in Nov 2026 and broke the parser) — a listing that can't be fetched
    or parsed must show a notice on Home, not a 500 for the whole page. Every
    outcome is recorded (services/source_health.py) so Settings can show it
    and the notice can say when the listing last worked."""
    try:
        bundles = await storefront.fetch_current_bundles(force=force)
    except Exception as exc:  # noqa: BLE001 - surfaced as a notice, never a crash
        logger.warning("Storefront listing unavailable: %s", exc)
        source_health.record_failure(db, source_health.SOURCE_STOREFRONT, exc)
        health = source_health.get(db, source_health.SOURCE_STOREFRONT)
        return [], "Couldn't read Humble's bundle listing right now (their page may have changed).", (
            health.last_success_at if health else None
        )
    source_health.record_success(db, source_health.SOURCE_STOREFRONT, at=storefront.last_fetched_at())
    return bundles, None, None


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    bundles, storefront_error, storefront_last_ok = await _fetch_listing_safe(db)
    tiles = await build_tiles(bundles, db)
    return templates.TemplateResponse(
        request,
        "home/index.html",
        {
            "by_category": grouped(tiles),
            "category_labels": CATEGORY_LABELS,
            "last_synced": storefront.last_fetched_at(),
            "storefront_error": storefront_error,
            "storefront_last_ok": storefront_last_ok,
            # Absence of the row, not its status — a saved-but-failing key means
            # the user has already been through setup and gets the error on the
            # Settings card instead of a getting-started banner here.
            "humble_configured": Credential.get(db, SOURCE_HUMBLE) is not None,
        },
    )


@router.post("/storefront/refresh", response_class=HTMLResponse, dependencies=[Depends(rate_limit(_refresh_limiter, "storefront-refresh")), Depends(require_csrf)])
async def refresh_storefront(request: Request, db: Session = Depends(get_db)):
    bundles, storefront_error, storefront_last_ok = await _fetch_listing_safe(db, force=True)
    tiles = await build_tiles(bundles, db)
    return templates.TemplateResponse(
        request,
        "home/_bundles.html",
        {
            "by_category": grouped(tiles),
            "category_labels": CATEGORY_LABELS,
            "last_synced": storefront.last_fetched_at(),
            "storefront_error": storefront_error,
            "storefront_last_ok": storefront_last_ok,
        },
    )


@router.get("/storefront/compare", response_class=HTMLResponse)
async def compare(request: Request, url: str, view: str = "list", db: Session = Depends(get_db)):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != _ALLOWED_STOREFRONT_HOST:
        raise HTTPException(status_code=400, detail="Invalid bundle URL")

    detail = await storefront.fetch_bundle_detail(url)
    catalog = build_catalog(db)

    def _row(item: storefront.StorefrontItem) -> dict:
        owned_entry = catalog.get(item.machine_name)
        avg_value = avg_item_value(owned_entry["bundles"]) if owned_entry else None
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
