"""Wishlist page.

Audible only for now. The context is shaped around a list of sources rather
than one so Steam (IWishlistService) and GOG (embed.gog.com/user/wishlist.json)
can be added as modules instead of a rewrite — both are reachable with
credentials this app already stores.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.models.audible_book import AudibleBook
from app.models.audible_wishlist import AudibleWishlistItem, AudibleWishlistPrice
from app.ratelimit import RateLimiter, rate_limit
from app.sync import audible_sync
from app.templates_env import templates

router = APIRouter(prefix="/wishlist")

_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

SORTS = {
    "added": AudibleWishlistItem.added_at.desc(),
    "title": AudibleWishlistItem.title.asc(),
    "price": AudibleWishlistItem.current_price.asc(),
}


def _lowest_prices(db: Session) -> dict[str, float]:
    """Cheapest observation ever recorded per title — the thing that makes a
    current price mean anything."""
    rows = (
        db.query(AudibleWishlistPrice.asin, func.min(AudibleWishlistPrice.price))
        .filter(AudibleWishlistPrice.price.isnot(None))
        .group_by(AudibleWishlistPrice.asin)
        .all()
    )
    return {asin: low for asin, low in rows if low is not None}


def _context(db: Session, q: str = "", sort: str = "added", deals_only: bool = False) -> dict:
    query = db.query(AudibleWishlistItem).filter(AudibleWishlistItem.removed_at.is_(None))
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                AudibleWishlistItem.title.ilike(like),
                AudibleWishlistItem.authors.ilike(like),
                AudibleWishlistItem.narrators.ilike(like),
            )
        )
    items = query.order_by(SORTS.get(sort, SORTS["added"])).all()

    if deals_only:
        items = [i for i in items if i.discount_pct]

    # An item can sit on the wishlist after being bought elsewhere; the owned
    # library is already synced, so flagging that costs one query.
    owned = {a for (a,) in db.query(AudibleBook.asin).all()}

    return {
        "items": items,
        "owned_asins": owned,
        "lowest_prices": _lowest_prices(db),
        "q": q,
        "sort": sort,
        "deals_only": deals_only,
        "total": db.query(AudibleWishlistItem).filter(AudibleWishlistItem.removed_at.is_(None)).count(),
        "deal_count": sum(
            1
            for i in db.query(AudibleWishlistItem).filter(AudibleWishlistItem.removed_at.is_(None)).all()
            if i.discount_pct
        ),
        "last_synced": db.query(func.max(AudibleWishlistItem.last_seen_at)).scalar(),
        "refresh_result": None,
        "refresh_error": None,
    }


@router.get("", response_class=HTMLResponse)
def wishlist_page(
    request: Request,
    q: str = "",
    sort: str = "added",
    deals_only: bool = False,
    db: Session = Depends(get_db),
):
    context = _context(db, q.strip(), sort, deals_only)
    template = "wishlist/_table.html" if request.headers.get("HX-Request") else "wishlist/index.html"
    return templates.TemplateResponse(request, template, context)


@router.post(
    "/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "wishlist-refresh")), Depends(require_csrf)],
)
async def refresh_wishlist(
    request: Request, q: str = Form(""), sort: str = Form("added"), db: Session = Depends(get_db)
):
    result = error = None
    try:
        result = await audible_sync.refresh_audible_wishlist(db)
    except audible_sync.NotConnectedError as exc:
        error = str(exc)
    except Exception as exc:
        # Undocumented endpoint on a personal account — a shape change should
        # read as a broken connector, not a broken app.
        error = f"The wishlist refresh failed ({type(exc).__name__}). Reconnect Audible in Settings if this persists."

    context = _context(db, q.strip(), sort)
    context["refresh_result"] = result
    context["refresh_error"] = error
    return templates.TemplateResponse(request, "wishlist/_table.html", context)
