"""Home page: shows Humble Bundle's currently-for-sale bundles (games, books,
software) pulled live from the public storefront (see connectors/storefront.py),
and a compare view that cross-references a for-sale bundle's contents against
the user's own Catalog via the shared machine_name identity key.
"""

from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import storefront
from app.csrf import require_csrf
from app.deps import get_db
from app.ratelimit import RateLimiter, rate_limit
from app.routers.catalog import _avg_item_value, _build_catalog
from app.templates_env import templates

router = APIRouter()

CATEGORY_LABELS = [("games", "Games"), ("books", "Books"), ("software", "Software")]
_ALLOWED_STOREFRONT_HOST = urlparse(storefront.BASE_URL).hostname
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)


def _grouped(bundles: list[storefront.StorefrontBundle]) -> dict[str, list]:
    by_category: dict[str, list] = {key: [] for key, _ in CATEGORY_LABELS}
    for b in bundles:
        by_category.setdefault(b.category, []).append(b)
    for group in by_category.values():
        group.sort(key=lambda b: b.start_date or datetime.min, reverse=True)
    return by_category


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    bundles = await storefront.fetch_current_bundles()
    return templates.TemplateResponse(
        request,
        "home/index.html",
        {"by_category": _grouped(bundles), "category_labels": CATEGORY_LABELS},
    )


@router.post("/storefront/refresh", response_class=HTMLResponse, dependencies=[Depends(rate_limit(_refresh_limiter, "storefront-refresh")), Depends(require_csrf)])
async def refresh_storefront(request: Request):
    bundles = await storefront.fetch_current_bundles(force=True)
    return templates.TemplateResponse(
        request,
        "home/_bundles.html",
        {"by_category": _grouped(bundles), "category_labels": CATEGORY_LABELS},
    )


@router.get("/storefront/compare", response_class=HTMLResponse)
async def compare(request: Request, url: str, db: Session = Depends(get_db)):
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
        },
    )
