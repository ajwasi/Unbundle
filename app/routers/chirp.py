"""Chirp Books — a synced, searchable/sortable view of the purchased
library, built on top of app/connectors/chirp_connector.py (see its own
docstring for what's confirmed vs. reconstructed vs. still unknown about
that API) and app/sync/chirp_sync.py (which walks every page of the
library and caches it locally, same "cached snapshot" shape as Steam/GOG/
Audible's own synced libraries — see chirp_sync.py's own docstring for why
that page-walk is written defensively).

No download support — confirmed live (2026-10-06) that Cloudflare blocks the
player page itself even with a cookie the same session already used
successfully elsewhere; see chirp_connector.py's own probe_first_track()
docstring. "Price" is Chirp's current storefront price for the book, not
what this account paid — see ChirpAudiobook's own docstring for why those
are different things and only one of them is confirmed to exist in Chirp's
API at all.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.list_views import render_list_or_partial, sorted_query
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.chirp_series_book import ChirpSeriesBook
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_CHIRP
from app.ratelimit import RateLimiter, rate_limit
from app.sync import chirp_sync
from app.templates_env import templates

router = APIRouter(prefix="/chirp")

# Same ceiling as Settings' own chirp-login limiter — a refresh performs the
# identical live, Cloudflare-fronted fetch against the same rate-limited path.
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

_SORT_COLUMNS = {
    "title": ChirpAudiobook.title,
    "author": ChirpAudiobook.authors,
    "narrator": ChirpAudiobook.narrators,
    "progress": ChirpAudiobook.position_percent,
    "price": func.coalesce(ChirpAudiobook.discount_price, ChirpAudiobook.listing_price),
    "purchased": ChirpAudiobook.purchased_at,
    "paid": ChirpAudiobook.paid_price,
}


def _context(db: Session, q: str = "", sort: str = "title", dir: str = "asc") -> dict:
    cred = Credential.get(db, SOURCE_CHIRP)
    query = db.query(ChirpAudiobook)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(ChirpAudiobook.title.ilike(like), ChirpAudiobook.authors.ilike(like), ChirpAudiobook.narrators.ilike(like))
        )
    books = sorted_query(query, _SORT_COLUMNS, sort, dir, ChirpAudiobook.title).all()

    owned_urls = {u for (u,) in db.query(ChirpAudiobook.url_path).all()}
    series_rows = db.query(ChirpSeriesBook).order_by(ChirpSeriesBook.series_name, ChirpSeriesBook.series_number).all()
    missing_series = [row for row in series_rows if row.url_path not in owned_urls]
    series_slug_by_path = {row.url_path: row.series_slug for row in series_rows}

    return {
        "chirp_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "chirp_error": cred.last_error if cred else None,
        "books": books,
        "missing_series": missing_series,
        "series_slug_by_path": series_slug_by_path,
        "book_count": db.query(func.count(ChirpAudiobook.purchase_id)).scalar() or 0,
        "q": q,
        "sort": sort,
        "dir": dir,
        "last_synced": db.query(func.max(ChirpAudiobook.fetched_at)).scalar(),
    }


@router.get("", response_class=HTMLResponse)
def chirp_page(request: Request, q: str = "", sort: str = "title", dir: str = "asc", db: Session = Depends(get_db)):
    context = _context(db, q, sort, dir)
    return render_list_or_partial(request, templates, "chirp/index.html", "chirp/_books_table.html", context)


def _series_sort_key(series_number: str) -> tuple:
    try:
        return (0, float(series_number))
    except (TypeError, ValueError):
        return (1, series_number or "")


@router.get("/series/{slug}", response_class=HTMLResponse)
def chirp_series_detail(request: Request, slug: str, db: Session = Depends(get_db)):
    series_url = f"/series/{slug}"
    rows = db.query(ChirpSeriesBook).filter(ChirpSeriesBook.series_url == series_url).all()
    if not rows:
        return templates.TemplateResponse(request, "chirp/series_not_found.html", {"slug": slug}, status_code=404)

    owned_urls = {u for (u,) in db.query(ChirpAudiobook.url_path).all()}
    table_rows = [
        {
            "owned": row.url_path in owned_urls,
            "url_path": row.url_path,
            "title": row.title,
            "authors": row.authors,
            "sequence": row.series_number,
            "current_price": row.current_price,
            "discount_pct": row.discount_pct,
        }
        for row in rows
    ]
    table_rows.sort(key=lambda r: _series_sort_key(r["sequence"]))

    return templates.TemplateResponse(
        request,
        "chirp/series_detail.html",
        {"series_name": rows[0].series_name, "slug": slug, "rows": table_rows},
    )


@router.post(
    "/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "chirp-refresh")), Depends(require_csrf)],
)
async def refresh_chirp(request: Request, db: Session = Depends(get_db)):
    try:
        await chirp_sync.refresh_chirp_library(db)
    except chirp_sync.NotConnectedError:
        pass  # nothing to fetch — the page already shows a "not configured" state
    except Exception:
        pass  # error already recorded on the credential row by refresh_chirp_library
    return templates.TemplateResponse(request, "chirp/_content.html", _context(db))
