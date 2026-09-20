"""Deals page.

GOG only for now, and named Deals rather than Deal of the Day because a
single flagged daily deal is not something the public catalogue exposes —
what it publishes is discounted products, which is both honest and more
useful.

Shaped around a list of sources so another can be added as a module. No
credential is involved: the GOG catalogue is public, and the only thing read
from the database is which products are already owned.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import gog_deals
from app.csrf import require_csrf
from app.deps import get_db
from app.models.gog_game import GogGame
from app.ratelimit import RateLimiter, rate_limit
from app.templates_env import templates

router = APIRouter(prefix="/deals")

_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

SORTS = {
    "discount": lambda d: -(d.discount_pct or 0),
    "price": lambda d: (d.price_final if d.price_final is not None else float("inf")),
    "title": lambda d: d.title.lower(),
    "rating": lambda d: -(d.rating or 0),
}


async def _context(
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
    shown = sorted(shown, key=SORTS.get(sort, SORTS["discount"]))

    return {
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


@router.get("", response_class=HTMLResponse)
async def deals_page(
    request: Request,
    q: str = "",
    sort: str = "discount",
    hide_owned: bool = False,
    min_discount: int = 0,
    db: Session = Depends(get_db),
):
    context = await _context(db, q.strip(), sort, hide_owned, min_discount)
    template = "deals/_table.html" if request.headers.get("HX-Request") else "deals/index.html"
    return templates.TemplateResponse(request, template, context)


@router.post(
    "/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "deals-refresh")), Depends(require_csrf)],
)
async def refresh_deals(
    request: Request,
    q: str = Form(""),
    sort: str = Form("discount"),
    hide_owned: bool = Form(False),
    min_discount: int = Form(0),
    db: Session = Depends(get_db),
):
    context = await _context(db, q.strip(), sort, hide_owned, min_discount, force=True)
    return templates.TemplateResponse(request, "deals/_table.html", context)
