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
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.gog_game import GogGame
from app.models.steam_game import SteamGame
from app.models.store_wishlist import (
    ChirpWishlistItem,
    ChirpWishlistPrice,
    GogWishlistItem,
    GogWishlistPrice,
    SteamWishlistItem,
    SteamWishlistPrice,
)
from app.ratelimit import RateLimiter, rate_limit
from app.services import source_health
from app.sync import audible_sync, store_wishlist_sync
from app.templates_env import templates

router = APIRouter(prefix="/wishlist")

_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

SOURCES = ("audible", "steam", "gog", "chirp")
SOURCE_LABELS = {"audible": "Audible", "steam": "Steam", "gog": "GOG", "chirp": "Chirp"}

_ITEM_MODELS = {
    "audible": (AudibleWishlistItem, "asin"),
    "steam": (SteamWishlistItem, "appid"),
    "gog": (GogWishlistItem, "product_id"),
    "chirp": (ChirpWishlistItem, "url_path"),
}
_PRICE_MODELS = {
    "audible": (AudibleWishlistPrice, "asin"),
    "steam": (SteamWishlistPrice, "appid"),
    "gog": (GogWishlistPrice, "product_id"),
    "chirp": (ChirpWishlistPrice, "url_path"),
}

# Column headers sort the table: click once for ascending, again for
# descending, a third time to clear back to the store's own order below.
# Values are the ascending basis; direction is applied in _context().
SORT_COLUMNS = {
    "audible": {
        "title": func.lower(AudibleWishlistItem.title),
        "author": func.lower(AudibleWishlistItem.authors),
        "rating": AudibleWishlistItem.rating,
        "price": AudibleWishlistItem.current_price,
    },
    "steam": {
        "title": func.lower(SteamWishlistItem.name),
        "author": func.lower(SteamWishlistItem.developers),
        "rating": SteamWishlistItem.metacritic,
        "price": SteamWishlistItem.current_price,
    },
    # No "author"/"rating" for GOG — its wishlist model carries neither field
    # (see GogWishlistItem's own docstring on the rating gap).
    "gog": {
        "title": func.lower(GogWishlistItem.title),
        "price": GogWishlistItem.current_price,
    },
    "chirp": {
        "title": func.lower(ChirpWishlistItem.title),
        "author": func.lower(ChirpWishlistItem.authors),
        "price": ChirpWishlistItem.current_price,
    },
}

# What "no sort" means: the order the store itself gives the list.
DEFAULT_ORDER = {
    "audible": AudibleWishlistItem.added_at.desc(),
    "steam": SteamWishlistItem.priority.asc(),
    "gog": GogWishlistItem.first_seen_at.desc(),
    "chirp": ChirpWishlistItem.first_seen_at.desc(),
}

# "lowest" sorts by _lowest()'s per-item historical-low price, which isn't a
# mapped column (it's aggregated separately from the price-history table), so
# _context() below special-cases it as a Python-side sort instead. Every
# source has the "Lowest seen" column, so every source can sort by it.
_LOWEST_SORT_KEY = "lowest"


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
    if source == "chirp":
        return {p for (p,) in db.query(ChirpAudiobook.url_path).all()}
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
    if source == "chirp":
        return query.filter(or_(ChirpWishlistItem.title.ilike(like), ChirpWishlistItem.authors.ilike(like)))
    return query.filter(GogWishlistItem.title.ilike(like))


def _context(
    db: Session,
    source: str,
    q: str = "",
    sort: str = "",
    dir: str = "asc",
    deals_only: bool = False,
    result: dict | None = None,
    error: str | None = None,
) -> dict:
    model, key_field = _ITEM_MODELS[source]
    query = db.query(model).filter(model.removed_at.is_(None))
    if q:
        query = _search(query, source, q)

    columns = SORT_COLUMNS[source]
    descending = dir == "desc"
    lowest_prices = _lowest(db, source)
    if sort == _LOWEST_SORT_KEY:
        items = query.all()
        priced = [i for i in items if getattr(i, key_field) in lowest_prices]
        unpriced = [i for i in items if getattr(i, key_field) not in lowest_prices]
        priced.sort(key=lambda i: lowest_prices[getattr(i, key_field)], reverse=descending)
        items = priced + unpriced  # unknown values last in either direction
    elif sort in columns:
        column = columns[sort]
        items = query.order_by(column.is_(None), column.desc() if descending else column.asc()).all()
    else:
        sort = ""
        items = query.order_by(DEFAULT_ORDER[source]).all()
    if deals_only:
        items = [i for i in items if i.discount_pct]

    counts = _counts(db)
    return {
        "items": items,
        "key_field": key_field,
        "owned_keys": _owned_keys(db, source),
        "lowest_prices": lowest_prices,
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
        "dir": "desc" if descending else "asc",
        "sortable": [*columns, _LOWEST_SORT_KEY],
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
    sort: str = "",
    dir: str = "asc",
    deals_only: bool = False,
    db: Session = Depends(get_db),
):
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail="Unknown wishlist source")
    context = _context(db, source, q.strip(), sort, dir, deals_only)
    template = "wishlist/_table.html" if request.headers.get("HX-Request") else "wishlist/index.html"
    return templates.TemplateResponse(request, template, context)


# Audible's wishlist is covered by its Credential status; the store wishlists
# are scrapes with no such thing, so their outcomes go to source_health.
_HEALTH_SOURCES = {
    "steam": source_health.SOURCE_WISHLIST_STEAM,
    "gog": source_health.SOURCE_WISHLIST_GOG,
    "chirp": source_health.SOURCE_WISHLIST_CHIRP,
}


def _record_health(db: Session, source: str, error: Exception | None = None) -> None:
    health_source = _HEALTH_SOURCES.get(source)
    if health_source is None:
        return
    if error is None:
        source_health.record_success(db, health_source)
    else:
        source_health.record_failure(db, health_source, error)


async def _run_refresh(source: str, db: Session):
    if source == "audible":
        return await audible_sync.refresh_audible_wishlist(db)
    if source == "steam":
        return await store_wishlist_sync.refresh_steam_wishlist(db)
    if source == "chirp":
        return await store_wishlist_sync.refresh_chirp_wishlist(db)
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
    sort: str = Form(""),
    dir: str = Form("asc"),
    deals_only: bool = Form(False),
    db: Session = Depends(get_db),
):
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail="Unknown wishlist source")

    result = error = None
    try:
        result = await _run_refresh(source, db)
    except (audible_sync.NotConnectedError, store_wishlist_sync.NotConnectedError) as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as an error message
        _record_health(db, source, exc)
        error = (
            f"The {SOURCE_LABELS[source]} wishlist refresh failed ({type(exc).__name__}). "
            "Reconnect it in Settings if this persists."
        )

    if result is not None:
        _record_health(db, source)
    context = _context(db, source, q.strip(), sort, dir, deals_only, result=result, error=error)
    return templates.TemplateResponse(request, "wishlist/_table.html", context)
