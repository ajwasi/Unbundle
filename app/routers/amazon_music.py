"""Purchased-music inventory page.

Read-only over the cached AmazonMusicTrack rows plus one refresh action.
Nothing here talks to Amazon except POST /amazon-music/refresh, which is rate
limited like every other outbound refresh in this app.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.connectors.amazon_music_connector import AmazonMusicAuthError
from app.csrf import require_csrf
from app.deps import get_db
from app.models.amazon_music_track import AmazonMusicTrack
from app.ratelimit import RateLimiter, rate_limit
from app.sync import amazon_music_sync
from app.templates_env import templates

router = APIRouter(prefix="/amazon-music")

_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

SORTS = {
    "added": AmazonMusicTrack.first_seen_at.desc(),
    "title": AmazonMusicTrack.title.asc(),
    "artist": AmazonMusicTrack.artist.asc(),
    "album": AmazonMusicTrack.album.asc(),
    "duration": AmazonMusicTrack.duration_seconds.asc(),
}

# "New since last refresh" is relative to the most recent sync, not a fixed
# window: a library synced once a month should still light up its new arrivals.
NEW_WINDOW = timedelta(minutes=5)


def _context(db: Session, q: str = "", sort: str = "added", show_missing: bool = False) -> dict:
    query = db.query(AmazonMusicTrack)
    if not show_missing:
        query = query.filter(AmazonMusicTrack.missing_since.is_(None))
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                AmazonMusicTrack.title.ilike(like),
                AmazonMusicTrack.artist.ilike(like),
                AmazonMusicTrack.album.ilike(like),
            )
        )
    tracks = query.order_by(SORTS.get(sort, SORTS["added"])).all()

    last_sync = db.query(AmazonMusicTrack.last_seen_at).order_by(AmazonMusicTrack.last_seen_at.desc()).first()
    last_synced = last_sync[0] if last_sync else None
    new_cutoff = (last_synced - NEW_WINDOW) if last_synced else None

    return {
        "tracks": tracks,
        "q": q,
        "sort": sort,
        "show_missing": show_missing,
        "total": db.query(AmazonMusicTrack).count(),
        "missing_count": db.query(AmazonMusicTrack).filter(AmazonMusicTrack.missing_since.isnot(None)).count(),
        "last_synced": last_synced,
        "new_cutoff": new_cutoff,
        "refresh_result": None,
        "refresh_error": None,
    }


@router.get("", response_class=HTMLResponse)
def amazon_music_page(
    request: Request,
    q: str = "",
    sort: str = "added",
    show_missing: bool = False,
    db: Session = Depends(get_db),
):
    context = _context(db, q.strip(), sort, show_missing)
    template = "amazon_music/_table.html" if request.headers.get("HX-Request") else "amazon_music/index.html"
    return templates.TemplateResponse(request, template, context)


@router.post(
    "/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "amazon-music-refresh")), Depends(require_csrf)],
)
def refresh_amazon_music(request: Request, q: str = Form(""), sort: str = Form("added"), db: Session = Depends(get_db)):
    result = error = None
    try:
        result = amazon_music_sync.refresh_purchased_tracks(db)
    except amazon_music_sync.NotConnectedError as exc:
        error = str(exc)
    except AmazonMusicAuthError as exc:
        error = str(exc)
    except Exception as exc:
        # An undocumented API on a personal account: a shape change is a
        # plausible outcome, and it should read as a broken connector rather
        # than a broken app.
        error = f"The refresh failed ({type(exc).__name__}). Amazon may have changed this API."

    context = _context(db, q.strip(), sort)
    context["refresh_result"] = result
    context["refresh_error"] = error
    return templates.TemplateResponse(request, "amazon_music/_table.html", context)
