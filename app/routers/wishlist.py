"""Wishlist pages — one per store.

Each store gets its own page rather than one merged table, because the stores
describe different things: an audiobook has a narrator and a five-star
average, a Steam game has a developer and a Metacritic score out of 100, and
GOG publishes no rating reachable by product id at all. Merging them meant
every column had to mean whatever the row's store said it meant.

/wishlist redirects to whichever store actually has items, so the page is
never an empty tab when another one is full.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.models.audible_book import AudibleBook
from app.models.audible_wishlist import AudibleWishlistItem, AudibleWishlistPrice
from app.models.gog_game import GogGame
from app.models.steam_game import SteamGame
from app.models.store_wishlist import (
    GogWishlistItem,
    GogWishlistPrice,
    SteamWishlistItem,
    SteamWishlistPrice,
)
from app.ratelimit import RateLimiter, rate_limit
from app.sync import audible_sync, store_wishlist_sync
from app.templates_env import templates

router = APIRouter(prefix="/wishlist")

_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

SOURCES = ("audible", "steam", "gog")
SOURCE_LABELS = {"audible": "Audible", "steam": "Steam", "gog": "GOG"}

_ITEM_MODELS = {
    "audible": (AudibleWishlistItem, "asin"),
    "steam": (SteamWishlistItem, "appid"),
    "gog": (GogWishlistItem, "product_id"),
}
_PRICE_MODELS = {
    "audible": (AudibleWishlistPrice, "asin"),
    "steam": (SteamWishlistPrice, "appid"),
    "gog": (GogWishlistPrice, "product_id"),
}

SORTS = {
    "audible": {
        "added": AudibleWishlistItem.added_at.desc(),
        "title": AudibleWishlistItem.title.asc(),
        "price": AudibleWishlistItem.current_price.asc(),
        "rating": AudibleWishlistItem.rating.desc(),
    },
    "steam": {
        "added": SteamWishlistItem.priority.asc(),
        "title": SteamWishlistItem.name.asc(),
        "price": SteamWishlistItem.current_price.asc(),
        "rating": SteamWishlistItem.metacritic.desc(),
    },
    "gog": {
        "added": GogWishlistItem.first_seen_at.desc(),
        "title": GogWishlistItem.title.asc(),
        "price": GogWishlistItem.current_price.asc(),
    },
}


def _counts(db: Session) -> dict[str, int]:
    out = {}
    for source, (model, _) in _ITEM_MODELS.items():
        out[source] = db.query(model).filter(model.removed_at.is_(None)).count()
    return out


def _lowest(db: Session, source: str) -> dict:
    price_model, owner_field = _PRICE_MODELS[source]
    owner = getattr(price_model, owner_field)
    rows = (
        db.query(owner, func.min(price_model.price))
        .filter(price_model.price.isnot(None))
        .group_by(owner)
        .all()
    )
    return {key: low for key, low in rows if low is not None}


def _owned_keys(db: Session, source: str) -> set:
    if source == "audible":
        return {a for (a,) in db.query(AudibleBook.asin).all()}
    if source == "steam":
        return {a for (a,) in db.query(SteamGame.appid).all()}
    return {p for (p,) in db.query(GogGame.product_id).all()}


def _search(query, source: str, needle: str):
    like = f"%{needle}%"
    if source == "audible":
        return query.filter(
            or_(
                AudibleWishlistItem.title.ilike(like),
                AudibleWishlistItem.authors.ilike(like),
                AudibleWishlistItem.narrators.ilike(like),
            )
        )
    if source == "steam":
        return query.filter(or_(SteamWishlistItem.name.ilike(like), SteamWishlistItem.developers.ilike(like)))
    return query.filter(GogWishlistItem.title.ilike(like))


def _context(
    db: Session,
    source: str,
    q: str = "",
    sort: str = "added",
    deals_only: bool = False,
    result: dict | None = None,
    error: str | None = None,
) -> dict:
    model, key_field = _ITEM_MODELS[source]
    query = db.query(model).filter(model.removed_at.is_(None))
    if q:
        query = _search(query, source, q)

    sorts = SORTS[source]
    items = query.order_by(sorts.get(sort, sorts["added"])).all()
    if deals_only:
        items = [i for i in items if i.discount_pct]

    counts = _counts(db)
    return {
        "items": items,
        "key_field": key_field,
        "owned_keys": _owned_keys(db, source),
        "lowest_prices": _lowest(db, source),
        "source": source,
        "sources": SOURCES,
        "source_labels": SOURCE_LABELS,
        "counts": counts,
        "total": counts[source],
        "deal_count": sum(
            1 for i in db.query(model).filter(model.removed_at.is_(None)).all() if i.discount_pct
        ),
        "q": q,
        "sort": sort,
        "sorts": list(sorts.keys()),
        "deals_only": deals_only,
        "last_synced": db.query(func.max(model.last_seen_at)).scalar(),
        "result": result,
        "error": error,
    }


@router.get("", response_class=HTMLResponse)
def wishlist_index(db: Session = Depends(get_db)):
    """Land on a store that has something in it, rather than a default tab
    that happens to be empty while another holds hundreds."""
    counts = _counts(db)
    target = next((s for s in SOURCES if counts[s]), SOURCES[0])
    return RedirectResponse(f"/wishlist/{target}", status_code=303)


@router.get("/{source}", response_class=HTMLResponse)
def wishlist_page(
    request: Request,
    source: str,
    q: str = "",
    sort: str = "added",
    deals_only: bool = False,
    db: Session = Depends(get_db),
):
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail="Unknown wishlist source")
    context = _context(db, source, q.strip(), sort, deals_only)
    template = "wishlist/_table.html" if request.headers.get("HX-Request") else "wishlist/index.html"
    return templates.TemplateResponse(request, template, context)


async def _run_refresh(source: str, db: Session):
    if source == "audible":
        return await audible_sync.refresh_audible_wishlist(db)
    if source == "steam":
        return await store_wishlist_sync.refresh_steam_wishlist(db)
    return await store_wishlist_sync.refresh_gog_wishlist(db)


@router.post(
    "/{source}/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "wishlist-refresh")), Depends(require_csrf)],
)
async def refresh_wishlist(
    request: Request,
    source: str,
    q: str = Form(""),
    sort: str = Form("added"),
    db: Session = Depends(get_db),
):
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail="Unknown wishlist source")

    result = error = None
    try:
        result = await _run_refresh(source, db)
    except (audible_sync.NotConnectedError, store_wishlist_sync.NotConnectedError) as exc:
        error = str(exc)
    except Exception as exc:
        error = (
            f"The {SOURCE_LABELS[source]} wishlist refresh failed ({type(exc).__name__}). "
            "Reconnect it in Settings if this persists."
        )

    context = _context(db, source, q.strip(), sort, result=result, error=error)
    return templates.TemplateResponse(request, "wishlist/_table.html", context)
