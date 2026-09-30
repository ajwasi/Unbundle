"""Deals page: GOG's public catalogue and Humble's currently-for-sale
bundles, one tab each — mirroring the Wishlist page's per-store-tab shape
and for the same reason (see wishlist.py's own docstring): a GOG deal is a
flat %-off single price, a Humble bundle is a pay-what-you-want tiered
listing, and there's nothing meaningful to share in one row/column set.

Unlike Wishlist's stores, GOG and Humble don't share enough filter/sort
shape to unify behind one dynamic /deals/{source} route either (GOG has
q/sort/hide_owned/min_discount; Humble's storefront listing has none of
that) — so each gets its own literal route instead of a shared handler.

No credential is involved for either: GOG's catalogue and Humble's
storefront listing are both public. "Connect" only ever means "match
against what you already own," never "read access" — same as GOG's
original single-source docstring already explained.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.connectors import gog_deals, storefront
from app.csrf import require_csrf
from app.deps import get_db
from app.models.gog_game import GogGame
from app.ratelimit import RateLimiter, rate_limit
from app.routers.home import CATEGORY_LABELS, _build_tiles, _grouped
from app.templates_env import templates

router = APIRouter(prefix="/deals")

_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

SOURCES = ("gog", "humble")
SOURCE_LABELS = {"gog": "GOG", "humble": "Humble Bundle"}

GOG_SORTS = {
    "discount": lambda d: -(d.discount_pct or 0),
    "price": lambda d: (d.price_final if d.price_final is not None else float("inf")),
    "title": lambda d: d.title.lower(),
    "rating": lambda d: -(d.rating or 0),
}


async def _gog_context(
    db: Session,
    q: str = "",
    sort: str = "discount",
    hide_owned: bool = False,
    min_discount: int = 0,
    force: bool = False,
    error: str | None = None,
) -> dict:
    deals: list[gog_deals.GogDeal] = []
    total_discounted = 0
    if error is None:
        try:
            deals, total_discounted = await gog_deals.fetch_deals(force=force)
        except Exception as exc:
            error = f"Could not reach GOG's catalogue ({type(exc).__name__}). It may be temporarily unavailable."

    # The whole point of the page: an exact product_id join, not title matching.
    owned = {product_id for (product_id,) in db.query(GogGame.product_id).all()}
    owned_count = sum(1 for d in deals if d.product_id in owned)

    shown = deals
    if hide_owned:
        shown = [d for d in shown if d.product_id not in owned]
    if min_discount:
        shown = [d for d in shown if (d.discount_pct or 0) >= min_discount]
    if q:
        needle = q.lower()
        shown = [
            d
            for d in shown
            if needle in d.title.lower() or any(needle in dev.lower() for dev in d.developers)
        ]
    shown = sorted(shown, key=GOG_SORTS.get(sort, GOG_SORTS["discount"]))

    return {
        "source": "gog",
        "sources": SOURCES,
        "source_labels": SOURCE_LABELS,
        "deals": shown,
        "owned_ids": owned,
        "owned_count": owned_count,
        "fetched_count": len(deals),
        "total_discounted": total_discounted,
        "gog_connected": bool(owned),
        "q": q,
        "sort": sort,
        "hide_owned": hide_owned,
        "min_discount": min_discount,
        "currency": deals[0].currency if deals else "",
        "error": error,
    }


async def _humble_context(db: Session, force: bool = False) -> dict:
    bundles = await storefront.fetch_current_bundles(force=force)
    tiles = await _build_tiles(bundles, db)
    return {
        "source": "humble",
        "sources": SOURCES,
        "source_labels": SOURCE_LABELS,
        "by_category": _grouped(tiles),
        "category_labels": CATEGORY_LABELS,
        "last_synced": storefront.last_fetched_at(),
    }


@router.get("", response_class=HTMLResponse)
async def deals_index():
    return RedirectResponse("/deals/gog", status_code=303)


@router.get("/gog", response_class=HTMLResponse)
async def gog_deals_page(
    request: Request,
    q: str = "",
    sort: str = "discount",
    hide_owned: bool = False,
    min_discount: int = 0,
    db: Session = Depends(get_db),
):
    context = await _gog_context(db, q.strip(), sort, hide_owned, min_discount)
    template = "deals/_table.html" if request.headers.get("HX-Request") else "deals/index.html"
    return templates.TemplateResponse(request, template, context)


@router.post(
    "/gog/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "deals-refresh")), Depends(require_csrf)],
)
async def refresh_gog_deals(
    request: Request,
    q: str = Form(""),
    sort: str = Form("discount"),
    hide_owned: bool = Form(False),
    min_discount: int = Form(0),
    db: Session = Depends(get_db),
):
    context = await _gog_context(db, q.strip(), sort, hide_owned, min_discount, force=True)
    return templates.TemplateResponse(request, "deals/_table.html", context)


@router.get("/humble", response_class=HTMLResponse)
async def humble_deals_page(request: Request, db: Session = Depends(get_db)):
    context = await _humble_context(db)
    template = "deals/_humble.html" if request.headers.get("HX-Request") else "deals/index.html"
    return templates.TemplateResponse(request, template, context)


@router.post(
    "/humble/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "deals-refresh")), Depends(require_csrf)],
)
async def refresh_humble_deals(request: Request, db: Session = Depends(get_db)):
    context = await _humble_context(db, force=True)
    return templates.TemplateResponse(request, "deals/_humble.html", context)
