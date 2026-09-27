"""Purchased-music inventory page.

Read-only over the cached AmazonMusicTrack rows plus a refresh action and the
download actions in app/amazon_music/downloader.py. Nothing here talks to
Amazon directly except POST /amazon-music/refresh, which is rate limited like
every other outbound refresh in this app — download requests go through the
sequential queue in downloader.py instead, which is its own throttle.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.amazon_music import downloader
from app.connectors.amazon_music_connector import AmazonMusicAuthError
from app.csrf import require_csrf
from app.deps import get_db
from app.models.amazon_music_destination import AmazonMusicDestination
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


def _destination_rows(db: Session) -> list[dict]:
    return [
        {"id": d.id, "name": d.name, "path": d.path, "is_default": d.is_default}
        for d in db.query(AmazonMusicDestination).order_by(AmazonMusicDestination.id).all()
    ]


def _destinations_context(db: Session) -> dict:
    return {"destinations": _destination_rows(db)}


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

    active_downloads = downloader.get_active_downloads(db)

    context = {
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
        "active_downloads": active_downloads,
        "active_download_ids": {a["download_id"] for a in active_downloads},
    }
    context.update(_destinations_context(db))
    return context


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
    except amazon_music_sync.AmazonMusicRequestError as exc:
        # Amazon's own words about what it disliked — the only useful
        # diagnostic for an undocumented API, and far better than the
        # exception class name.
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


@router.post("/destinations", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def create_destination(request: Request, name: str = Form(default=""), path: str = Form(default=""), db: Session = Depends(get_db)):
    if name.strip() and path.strip():
        # The first destination anyone adds becomes the default automatically
        # — otherwise a user who configures exactly one directory would still
        # need a second click to actually use it.
        is_first = db.query(AmazonMusicDestination).count() == 0
        db.add(AmazonMusicDestination(name=name.strip(), path=path.strip(), is_default=is_first))
        db.commit()
    return templates.TemplateResponse(request, "amazon_music/_destinations.html", _destinations_context(db))


@router.post("/destinations/{destination_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def delete_destination(request: Request, destination_id: int, db: Session = Depends(get_db)):
    dest = db.get(AmazonMusicDestination, destination_id)
    if dest is not None:
        db.delete(dest)
        db.commit()
    return templates.TemplateResponse(request, "amazon_music/_destinations.html", _destinations_context(db))


@router.post("/destinations/{destination_id}/set-default", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def set_default_destination(request: Request, destination_id: int, db: Session = Depends(get_db)):
    dest = db.get(AmazonMusicDestination, destination_id)
    if dest is not None:
        db.query(AmazonMusicDestination).update({"is_default": False})
        dest.is_default = True
        db.commit()
    return templates.TemplateResponse(request, "amazon_music/_destinations.html", _destinations_context(db))


@router.post("/downloads", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def trigger_downloads(
    request: Request,
    download_id: list[str] = Form(default=[]),
    q: str = Form(""),
    sort: str = Form("added"),
    show_missing: bool = Form(False),
    db: Session = Depends(get_db),
):
    """A checked-boxes selection queues just those; an empty selection queues
    every track the *current* filter/search shows — mirrors bundles.py's own
    trigger_download (`items or None` means "no filter, download all"), which
    is also why "Download all" is the same form/button clearing its
    checkboxes on click rather than a separate action.
    """
    if download_id:
        await downloader.queue_many(db, download_id)
    else:
        query = db.query(AmazonMusicTrack.download_id)
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
        await downloader.queue_many(db, [row[0] for row in query.all()])
    return templates.TemplateResponse(request, "amazon_music/_table.html", _context(db, q.strip(), sort, show_missing))


@router.get("/downloads/active", response_class=HTMLResponse)
def active_downloads(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "amazon_music/_active_downloads.html", {"active_downloads": downloader.get_active_downloads(db)}
    )


# Declared after /downloads/active on purpose: Starlette matches routes in
# declaration order, not by specificity, so this dynamic segment would
# otherwise shadow the literal /downloads/active route above it.
@router.post("/downloads/{download_id}", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def download_track(
    request: Request, download_id: str, q: str = Form(""), sort: str = Form("added"), db: Session = Depends(get_db)
):
    try:
        await downloader.start_download(db, download_id)
    except downloader.UnknownTrackError:
        # A stale row from before a re-sync — nothing sane to do but ignore
        # the click; the table this renders won't offer that button once it
        # no longer lists the row.
        pass
    return templates.TemplateResponse(request, "amazon_music/_table.html", _context(db, q.strip(), sort))
