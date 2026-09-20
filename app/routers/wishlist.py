"""Wishlist page, across Audible, Steam and GOG.

Each source keeps its own table (see models/store_wishlist.py), so this
normalises them into one row shape rather than the template learning three.
Adding a fourth store means one more `_rows_*` function and an entry in
SOURCES — nothing else here changes.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session
from fastapi.responses import HTMLResponse

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


def _lowest(db: Session, price_model, owner_field: str) -> dict:
    owner = getattr(price_model, owner_field)
    rows = (
        db.query(owner, func.min(price_model.price))
        .filter(price_model.price.isnot(None))
        .group_by(owner)
        .all()
    )
    return {key: low for key, low in rows if low is not None}


def _row(source, key, title, subtitle, cover, url, item, owned, lowest, added_at, discount):
    return {
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "key": key,
        "title": title,
        "subtitle": subtitle,
        "cover": cover,
        "url": url,
        "current_price": item.current_price,
        "list_price": item.list_price,
        "currency": item.currency,
        "discount_pct": discount,
        "lowest": lowest,
        "owned": owned,
        "added_at": added_at,
    }


def _rows_audible(db: Session) -> list[dict]:
    lows = _lowest(db, AudibleWishlistPrice, "asin")
    owned = {a for (a,) in db.query(AudibleBook.asin).all()}
    items = db.query(AudibleWishlistItem).filter(AudibleWishlistItem.removed_at.is_(None)).all()
    return [
        _row(
            "audible",
            i.asin,
            i.title,
            i.authors,
            i.cover_url,
            f"https://www.audible.com/pd/{i.asin}",
            i,
            i.asin in owned,
            lows.get(i.asin),
            i.added_at,
            i.discount_pct,
        )
        for i in items
    ]


def _rows_steam(db: Session) -> list[dict]:
    lows = _lowest(db, SteamWishlistPrice, "appid")
    owned = {a for (a,) in db.query(SteamGame.appid).all()}
    items = db.query(SteamWishlistItem).filter(SteamWishlistItem.removed_at.is_(None)).all()
    return [
        _row(
            "steam",
            i.appid,
            i.name or f"App {i.appid}",
            i.developers,
            i.header_image,
            f"https://store.steampowered.com/app/{i.appid}/",
            i,
            i.appid in owned,
            lows.get(i.appid),
            i.added_at,
            i.discount_pct or None,
        )
        for i in items
    ]


def _rows_gog(db: Session) -> list[dict]:
    lows = _lowest(db, GogWishlistPrice, "product_id")
    owned = {p for (p,) in db.query(GogGame.product_id).all()}
    items = db.query(GogWishlistItem).filter(GogWishlistItem.removed_at.is_(None)).all()
    return [
        _row(
            "gog",
            i.product_id,
            i.title or f"Product {i.product_id}",
            "",
            i.cover_url,
            i.store_url or f"https://www.gog.com/game/{i.product_id}",
            i,
            i.product_id in owned,
            lows.get(i.product_id),
            None,
            i.discount_pct,
        )
        for i in items
    ]


SORTS = {
    "added": lambda r: (r["added_at"] is None, -(r["added_at"] or datetime.min).timestamp() if r["added_at"] else 0),
    "title": lambda r: r["title"].lower(),
    "price": lambda r: r["current_price"] if r["current_price"] is not None else float("inf"),
    "discount": lambda r: -(r["discount_pct"] or 0),
}


def _context(
    db: Session,
    q: str = "",
    sort: str = "added",
    source: str = "",
    deals_only: bool = False,
    results: dict | None = None,
    errors: dict | None = None,
) -> dict:
    rows = _rows_audible(db) + _rows_steam(db) + _rows_gog(db)

    counts = {s: sum(1 for r in rows if r["source"] == s) for s in SOURCES}
    deal_count = sum(1 for r in rows if r["discount_pct"])

    shown = rows
    if source in SOURCES:
        shown = [r for r in shown if r["source"] == source]
    if deals_only:
        shown = [r for r in shown if r["discount_pct"]]
    if q:
        needle = q.lower()
        shown = [r for r in shown if needle in r["title"].lower() or needle in (r["subtitle"] or "").lower()]
    shown = sorted(shown, key=SORTS.get(sort, SORTS["added"]))

    return {
        "rows": shown,
        "total": len(rows),
        "counts": counts,
        "deal_count": deal_count,
        "sources": SOURCES,
        "source_labels": SOURCE_LABELS,
        "q": q,
        "sort": sort,
        "source": source,
        "deals_only": deals_only,
        "results": results or {},
        "errors": errors or {},
    }


@router.get("", response_class=HTMLResponse)
def wishlist_page(
    request: Request,
    q: str = "",
    sort: str = "added",
    source: str = "",
    deals_only: bool = False,
    db: Session = Depends(get_db),
):
    context = _context(db, q.strip(), sort, source, deals_only)
    template = "wishlist/_table.html" if request.headers.get("HX-Request") else "wishlist/index.html"
    return templates.TemplateResponse(request, template, context)


async def _run_refresh(source: str, db: Session):
    if source == "audible":
        return await audible_sync.refresh_audible_wishlist(db)
    if source == "steam":
        return await store_wishlist_sync.refresh_steam_wishlist(db)
    return await store_wishlist_sync.refresh_gog_wishlist(db)


@router.post(
    "/refresh/{source}",
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

    results: dict = {}
    errors: dict = {}
    try:
        results[source] = await _run_refresh(source, db)
    except (audible_sync.NotConnectedError, store_wishlist_sync.NotConnectedError) as exc:
        errors[source] = str(exc)
    except Exception as exc:
        # Three undocumented-ish sources; a shape change should read as one
        # broken connector, not a broken page.
        errors[source] = (
            f"The {SOURCE_LABELS[source]} wishlist refresh failed ({type(exc).__name__}). "
            "Reconnect it in Settings if this persists."
        )

    context = _context(db, q.strip(), sort, results=results, errors=errors)
    return templates.TemplateResponse(request, "wishlist/_table.html", context)
