"""Purchased-music inventory page.

Read-only over the cached AmazonMusicTrack rows plus a refresh action and the
download actions in app/amazon_music/downloader.py. Nothing here talks to
Amazon directly except POST /amazon-music/refresh, which is rate limited like
every other outbound refresh in this app — download requests go through the
sequential queue in downloader.py instead, which is its own throttle.
"""

import logging
import mimetypes
import re
import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import asset_cache
from app.amazon_music import downloader
from app.connectors.amazon_music_connector import AmazonMusicAuthError
from app.csrf import require_csrf
from app.deps import get_db
from app.models.amazon_music_album_catalog_sync import AmazonMusicAlbumCatalogSync
from app.models.amazon_music_destination import AmazonMusicDestination
from app.models.amazon_music_track import AmazonMusicTrack
from app.ratelimit import RateLimiter, rate_limit
from app.sync import amazon_music_sync
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/amazon-music")

_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

SORTS = {
    "added": AmazonMusicTrack.first_seen_at.desc(),
    "title": AmazonMusicTrack.title.asc(),
    "artist": AmazonMusicTrack.artist.asc(),
    "album": AmazonMusicTrack.album.asc(),
    "duration": AmazonMusicTrack.duration_seconds.asc(),
    # Unnumbered tracks (no on-demand catalog fetch has run, or this one
    # wasn't matched) sort last rather than first — SQLite's own default put
    # NULLs first, ahead of every real track number, which is backwards for
    # this specific sort.
    "track_number": AmazonMusicTrack.track_number.asc().nulls_last(),
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


# Never a real ASIN (those are always exactly 10 alphanumeric characters —
# see amc.ASIN_RE), so this can never collide with a real album_asin. Tracks
# land here when their own showPurchasedTracks row carried no album deep
# link at all — grouped together since there is no other stable identity to
# key them by.
_NO_ALBUM_KEY = "none"


def _album_key(album_asin: str) -> str:
    return album_asin or _NO_ALBUM_KEY


def _album_asin_filter(album_key: str):
    if album_key == _NO_ALBUM_KEY:
        return AmazonMusicTrack.album_asin == ""
    return AmazonMusicTrack.album_asin == album_key


def _album_summary(db: Session, album_key: str) -> dict | None:
    rows = db.query(AmazonMusicTrack).filter(_album_asin_filter(album_key)).all()
    if not rows:
        return None
    # Same freshness-over-first-seen picking as _album_rows — a presigned
    # cover_url expires, and an arbitrarily-picked row's own one may already
    # have, even while another track in the same album still has a live one.
    cover_url = ""
    cover_refreshed_at = None
    for t in rows:
        if not t.cover_url:
            continue
        refreshed = t.cover_url_refreshed_at or t.first_seen_at
        if not cover_url or refreshed > cover_refreshed_at:
            cover_url = t.cover_url
            cover_refreshed_at = refreshed
    row = rows[0]
    return {
        "album_key": album_key,
        "album": row.album or "(Unknown album)",
        "artist": "Various Artists" if row.is_compilation else row.artist,
        "cover_url": cover_url,
    }


_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n]')


def _safe_filename(name: str, suffix: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", name).strip(" .") or "cover"
    return f"{cleaned[:80]}{suffix}"


# Cheap in-process cache for "which URL is the freshest known cover for this
# album", mirroring catalog.py's own _catalog_cache — the /cover route is hit
# once per <img> tag (up to _ALBUMS_PAGE_SIZE per grid page), and without this
# each of those was re-running the same full per-album track scan that
# _album_rows already does for the grid itself, purely to re-derive a URL
# that, on a warm cache, isn't even needed.
_cover_cache: dict = {"key": None, "info": None}
# Guards _cover_cache — album_cover() runs on FastAPI's sync threadpool, so
# concurrent requests genuinely can race the check-then-write below (not
# corruption, since both threads compute the same correct answer, but real
# duplicated query work worth just not having).
_cover_cache_lock = threading.Lock()


def _cover_cache_key(db: Session):
    return db.query(
        func.count(AmazonMusicTrack.download_id),
        func.max(AmazonMusicTrack.cover_url_refreshed_at),
        func.max(AmazonMusicTrack.first_seen_at),
    ).one()


def _resolve_cover_info(db: Session) -> dict[str, dict]:
    """album_key -> {"url", "album"} for every album with a known cover,
    picking the same freshest-by-cover_url_refreshed_at candidate _album_rows
    and _album_summary do, recomputed only when the underlying track data
    actually changes.
    """
    key = _cover_cache_key(db)
    with _cover_cache_lock:
        if _cover_cache["key"] == key:
            return _cover_cache["info"]

    info: dict[str, dict] = {}
    refreshed_at: dict[str, datetime] = {}
    columns = (
        AmazonMusicTrack.album_asin,
        AmazonMusicTrack.album,
        AmazonMusicTrack.cover_url,
        AmazonMusicTrack.cover_url_refreshed_at,
        AmazonMusicTrack.first_seen_at,
    )
    for album_asin, album, cover_url, cover_url_refreshed_at, first_seen_at in db.query(*columns).all():
        if not cover_url:
            continue
        album_key = _album_key(album_asin)
        refreshed = cover_url_refreshed_at or first_seen_at
        if album_key not in info or refreshed > refreshed_at[album_key]:
            info[album_key] = {"url": cover_url, "album": album or "(Unknown album)"}
            refreshed_at[album_key] = refreshed

    with _cover_cache_lock:
        _cover_cache["key"] = key
        _cover_cache["info"] = info
    return info


def _catalog_sync_status(db: Session, album_key: str) -> dict | None:
    """Whether/when this album's real track order was last fetched on demand
    — None for the no-album-asin bucket (there is no single catalog album to
    look up for a grouping of tracks that each lack one) and for an album
    that has never been synced this way at all.
    """
    if album_key == _NO_ALBUM_KEY:
        return None
    row = db.get(AmazonMusicAlbumCatalogSync, album_key)
    if row is None:
        return None
    return {"synced_at": row.synced_at, "tracks_found": row.tracks_found, "tracks_matched": row.tracks_matched}


def _context(db: Session, q: str = "", sort: str = "added", show_missing: bool = False, album_key: str = "") -> dict:
    base_query = db.query(AmazonMusicTrack)
    if album_key:
        base_query = base_query.filter(_album_asin_filter(album_key))

    query = base_query
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

    last_track = base_query.order_by(AmazonMusicTrack.last_seen_at.desc()).first()
    new_cutoff = (last_track.last_seen_at - NEW_WINDOW) if last_track else None

    active_downloads = downloader.get_active_downloads(db)

    return {
        "tracks": tracks,
        "q": q,
        "sort": sort,
        "show_missing": show_missing,
        "album_key": album_key,
        "album_summary": _album_summary(db, album_key) if album_key else None,
        "catalog_sync": _catalog_sync_status(db, album_key) if album_key else None,
        "total": base_query.count(),
        "missing_count": base_query.filter(AmazonMusicTrack.missing_since.isnot(None)).count(),
        "new_cutoff": new_cutoff,
        "active_downloads": active_downloads,
        "active_download_ids": {a["download_id"] for a in active_downloads},
    }


# ------------------------------------------------------------------ albums

ALBUM_SORTS = {"added", "album", "artist", "tracks"}

# Mirrors catalog.py's own _PAGE_SIZE and the reasoning behind it: rendering
# every album row unpaginated is the actual cost that scales badly, not the
# Python-side grouping (benchmarked separately at ~450ms for 1,500 albums —
# real, but not the dominant cost at that scale, the same shape catalog.py
# already measured and fixed for its own unpaginated table).
_ALBUMS_PAGE_SIZE = 100


def _album_rows(db: Session, q: str = "", show_missing: bool = False) -> list[dict]:
    """Groups tracks into albums in Python, not a SQL GROUP BY.

    A personal purchased library tops out somewhere in the tens of thousands
    of rows at most — well within "load them all and group with a dict"
    territory — and doing it here avoids a GROUP BY query that has no clean
    way to express "pick the freshest non-blank cover" or "is any track in
    this album currently downloading" without real aggregate-function
    hackiness.
    """
    query = db.query(AmazonMusicTrack)
    if not show_missing:
        query = query.filter(AmazonMusicTrack.missing_since.is_(None))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(AmazonMusicTrack.album.ilike(like), AmazonMusicTrack.artist.ilike(like)))

    active_ids = {a["download_id"] for a in downloader.get_active_downloads(db)}
    # One query for every album's sync status, not one per album — this
    # function already loads every matching track in a single query for the
    # same reason.
    order_synced_asins = {row[0] for row in db.query(AmazonMusicAlbumCatalogSync.album_asin).all()}

    groups: dict[str, dict] = {}
    # cover_url is a presigned, expiring S3 URL (see AmazonMusicTrack's own
    # docstring) — picking merely the first non-blank one seen, with no
    # regard for how recently it was refreshed, can land on one that expired
    # sync-runs ago even while another track in the same album has a fresh
    # one. cover_refreshed_at tracks the winning pick's own timestamp per
    # album so a later, fresher track can still displace an earlier pick.
    cover_refreshed_at: dict[str, datetime] = {}
    for t in query.all():
        key = _album_key(t.album_asin)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "album_key": key,
                "album": t.album or "(Unknown album)",
                "artist": "Various Artists" if t.is_compilation else t.artist,
                "cover_url": "",
                "track_count": 0,
                "missing_count": 0,
                "downloading": False,
                "track_order_synced": key in order_synced_asins,
                "first_seen_at": t.first_seen_at,
            }
        g["track_count"] += 1
        if t.missing_since:
            g["missing_count"] += 1
        if t.cover_url:
            refreshed = t.cover_url_refreshed_at or t.first_seen_at
            if not g["cover_url"] or refreshed > cover_refreshed_at[key]:
                g["cover_url"] = t.cover_url
                cover_refreshed_at[key] = refreshed
        if t.download_id in active_ids:
            g["downloading"] = True
        if t.first_seen_at > g["first_seen_at"]:
            g["first_seen_at"] = t.first_seen_at
    return list(groups.values())


def _single_album_row(db: Session, album_key: str) -> dict | None:
    """Same row shape _album_rows builds, scoped to exactly one album — used
    to re-render a single grid row in place after an action on it, instead of
    re-rendering (and re-fetching/re-sorting) the entire grid just to reflect
    one row's new state.
    """
    rows = db.query(AmazonMusicTrack).filter(_album_asin_filter(album_key)).all()
    if not rows:
        return None
    active_ids = {a["download_id"] for a in downloader.get_active_downloads(db)}
    synced = album_key != _NO_ALBUM_KEY and db.get(AmazonMusicAlbumCatalogSync, album_key) is not None

    cover_url = ""
    cover_refreshed_at = None
    track_count = 0
    missing_count = 0
    downloading = False
    for t in rows:
        track_count += 1
        if t.missing_since:
            missing_count += 1
        if t.cover_url:
            refreshed = t.cover_url_refreshed_at or t.first_seen_at
            if not cover_url or refreshed > cover_refreshed_at:
                cover_url = t.cover_url
                cover_refreshed_at = refreshed
        if t.download_id in active_ids:
            downloading = True

    row = rows[0]
    return {
        "album_key": album_key,
        "album": row.album or "(Unknown album)",
        "artist": "Various Artists" if row.is_compilation else row.artist,
        "cover_url": cover_url,
        "track_count": track_count,
        "missing_count": missing_count,
        "downloading": downloading,
        "track_order_synced": synced,
    }


def _sort_albums(albums: list[dict], sort: str) -> list[dict]:
    # Every branch breaks ties on album_key. Without it, two albums sharing
    # the exact same primary sort value (easy for "added" — a whole sync run
    # can share one first_seen_at) fall back to whatever order the grouping
    # dict happened to produce, which itself isn't guaranteed by the
    # underlying (unordered) query — meaning which album lands on which
    # infinite-scroll page boundary could shift between requests. A stable
    # tiebreaker doesn't fix pagination drift from concurrent writes changing
    # an album's own sort value between batches (that would need real
    # cursor-based pagination), but it does remove ambiguity between albums
    # whose sort value never changes.
    if sort == "album":
        return sorted(albums, key=lambda a: (a["album"].lower(), a["album_key"]))
    if sort == "artist":
        return sorted(albums, key=lambda a: ((a["artist"] or "").lower(), a["album_key"]))
    if sort == "tracks":
        return sorted(albums, key=lambda a: (a["track_count"], a["album_key"]), reverse=True)
    return sorted(albums, key=lambda a: (a["first_seen_at"], a["album_key"]), reverse=True)  # "added", the default


def _albums_context(
    db: Session, q: str = "", sort: str = "added", show_missing: bool = False, offset: int = 0
) -> dict:
    # Belt-and-braces: the route itself rejects a negative offset (Query(...,
    # ge=0)), but clamping here too means this function can never misbehave
    # on a negative slice (Python's own negative-index slicing silently gives
    # confusing results — e.g. [-50:50] can come out empty) no matter how a
    # future caller invokes it.
    offset = max(0, offset)
    all_albums = _sort_albums(_album_rows(db, q, show_missing), sort)
    albums = all_albums[offset : offset + _ALBUMS_PAGE_SIZE]

    last_sync = db.query(AmazonMusicTrack.last_seen_at).order_by(AmazonMusicTrack.last_seen_at.desc()).first()
    last_synced = last_sync[0] if last_sync else None

    context = {
        "albums": albums,
        "q": q,
        "sort": sort,
        "show_missing": show_missing,
        "has_more": offset + _ALBUMS_PAGE_SIZE < len(all_albums),
        "next_offset": offset + _ALBUMS_PAGE_SIZE,
        "total_albums": len(all_albums),
        "shown_so_far": offset + len(albums),
        "total_tracks": db.query(AmazonMusicTrack).count(),
        "missing_count": db.query(AmazonMusicTrack).filter(AmazonMusicTrack.missing_since.isnot(None)).count(),
        "last_synced": last_synced,
        "refresh_result": None,
        "refresh_error": None,
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
    context = _albums_context(db, q.strip(), sort, show_missing)
    template = "amazon_music/_albums_table.html" if request.headers.get("HX-Request") else "amazon_music/index.html"
    return templates.TemplateResponse(request, template, context)


@router.get("/albums/rows", response_class=HTMLResponse)
def amazon_music_album_rows(
    request: Request,
    q: str = "",
    sort: str = "added",
    show_missing: bool = False,
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """One infinite-scroll batch for the albums grid — mirrors catalog.py's
    own GET /catalog/rows. Declared before /albums/{album_key} on purpose,
    same reason /downloads/active precedes /downloads/{download_id} below:
    Starlette matches routes in declaration order, and a dynamic segment
    declared first would swallow this literal path instead.
    """
    context = _albums_context(db, q.strip(), sort, show_missing, offset)
    return templates.TemplateResponse(request, "amazon_music/_albums_rows_batch.html", context)


@router.get("/albums/{album_key}", response_class=HTMLResponse)
def album_detail_page(
    request: Request,
    album_key: str,
    q: str = "",
    sort: str = "added",
    show_missing: bool = False,
    db: Session = Depends(get_db),
):
    context = _context(db, q.strip(), sort, show_missing, album_key)
    if context["album_summary"] is None:
        # Not "no tracks match the current filter" (that's a normal empty
        # search) — no track has this album_key at all, regardless of filter,
        # which means either a stale bookmark or the album was fully removed
        # in a later sync.
        return templates.TemplateResponse(
            request, "amazon_music/album_not_found.html", {"album_key": album_key}, status_code=404
        )
    template = "amazon_music/_table.html" if request.headers.get("HX-Request") else "amazon_music/album_detail.html"
    return templates.TemplateResponse(request, template, context)


_COVER_CACHE_HEADERS = {
    # The cache key is content-addressed (host+path of the remote URL, not
    # its query string — see asset_cache.py), so the file behind a given
    # /cover URL never changes without the URL itself changing too. Safe to
    # tell the browser to never re-request or re-validate it — with one real
    # tradeoff: nothing here ever automatically re-fetches an already-cached
    # file (get_or_fetch only ever fills a gap, warm-up included), so if
    # Amazon ever serves genuinely new art at the exact same URL path, both
    # this server's cache and every browser that's already loaded it are
    # stuck on the old image until someone notices and clicks "Refresh
    # cover" (below) — there's no automatic staleness detection for that
    # case, only for the presigned-URL-expiry problem this feature exists to
    # solve.
    "Cache-Control": "public, max-age=31536000, immutable"
}


@router.get("/albums/{album_key}/cover")
def album_cover(album_key: str, db: Session = Depends(get_db)):
    """Serves the album's cover art from this app's own local cache instead
    of Amazon's presigned, expiring URL directly — the same freshness-picked
    remote_url _album_rows already resolves for the grid (via the shared
    _resolve_cover_info cache, so this doesn't re-scan every track in the
    album just to find the URL), fetched once and served from disk on every
    request after that rather than re-embedding a URL that stops working
    sometime after the sync that captured it.
    """
    info = _resolve_cover_info(db).get(album_key)
    remote_url = info["url"] if info else ""
    path = asset_cache.get_or_fetch("amazon-music-album", remote_url)
    if path is None:
        return Response(status_code=404)
    try:
        # Read eagerly here rather than handing FileResponse a Path it
        # streams lazily later in the ASGI response lifecycle — a concurrent
        # "Refresh cover" call could evict this exact file in the narrow
        # window between get_or_fetch resolving it and FileResponse actually
        # opening it, which would otherwise surface as an unhandled
        # FileNotFoundError instead of a clean 404. Cover images are small
        # enough that reading fully into memory here costs nothing real.
        data = path.read_bytes()
    except OSError:
        return Response(status_code=404)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    filename = _safe_filename(info["album"], path.suffix) if info else path.name
    headers = dict(_COVER_CACHE_HEADERS)
    headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return Response(content=data, media_type=media_type, headers=headers)


_refresh_cover_limiter = RateLimiter(max_calls=10, period_seconds=60)


@router.post(
    "/albums/{album_key}/refresh-cover",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_cover_limiter, "amazon-music-refresh-cover")), Depends(require_csrf)],
)
def refresh_album_cover(request: Request, album_key: str, db: Session = Depends(get_db)):
    """Evicts the cached file for this album's current cover URL and returns
    a fresh <img> tag pointing at the same /cover URL — for a cache entry
    known to be wrong (a bad fetch that slipped through, or the underlying
    art genuinely changed at Amazon under the same URL path). A cache-busting
    query string is required on the new <img src> because the /cover
    response itself now tells the browser to cache the old one forever (see
    _COVER_CACHE_HEADERS) — without it the browser would keep showing the
    stale image from its own cache even after the server-side one is gone.

    Rate limited like /refresh — this triggers a real outbound fetch on its
    next view, and eviction also clears the failure cooldown, so an
    unthrottled version of this route would double as a way to force-bypass
    that cooldown by spamming the button.
    """
    info = _resolve_cover_info(db).get(album_key)
    if info:
        asset_cache.evict("amazon-music-album", info["url"], reason="user requested a refresh")
    return templates.TemplateResponse(
        request, "amazon_music/_album_cover_img.html", {"album_key": album_key, "cache_bust": int(time.time())}
    )


# Only paced when a warm-up iteration actually issues a real fetch (see
# below) — a re-sync where covers are already warm runs this loop at full
# speed, since every iteration is then just a fast disk check. The pacing
# only matters for albums that need a genuinely new fetch, which is exactly
# when it's needed: it keeps a large first-time warm-up from bursting
# requests at Amazon's CDN all at once (risking Amazon's own rate limiting,
# which would otherwise cascade into every remaining album in the same run
# looking "recently failed" to the cooldown above even though nothing was
# really wrong with them) and leaves room for anything else — a concurrent
# user-triggered download, say — making outbound calls at the same time.
_WARM_UP_PACING_SECONDS = 0.2

# Guards against two overlapping warm-up passes — e.g. two syncs triggered
# in quick succession — both walking the whole library's covers at once.
# acquire(blocking=False) below means a second call simply skips rather than
# queueing up behind the first.
_warm_up_lock = threading.Lock()


def _albums_with_visible_tracks(db: Session) -> set[str]:
    """album_keys with at least one non-missing track — warming a cover for
    an album that's entirely hidden by default (every one of its tracks has
    missing_since set) spends a real fetch on art nobody will see unless
    they flip on "show tracks no longer listed".
    """
    rows = db.query(AmazonMusicTrack.album_asin).filter(AmazonMusicTrack.missing_since.is_(None)).distinct().all()
    return {_album_key(album_asin) for (album_asin,) in rows}


def _warm_album_covers() -> None:
    """Best-effort, fire-and-forget cover pre-fetch kicked off right after a
    successful sync — without this, the *first* time anyone opens the albums
    grid after a fresh sync, up to _ALBUMS_PAGE_SIZE covers are all cold at
    once, and the grid's own <img> requests synchronously fetch them one by
    one through this app's request-handling threadpool. Warming them here
    instead means most are already cached by the time anyone looks.

    Runs after the request that scheduled it has already returned its
    response, so it opens its own DB session rather than reusing the
    request-scoped one, which may already be closed by then — and closes it
    again immediately once it has the list of URLs to warm, rather than
    holding it open for however long the (possibly slow, possibly paced)
    network fetches that follow take.
    """
    if not _warm_up_lock.acquire(blocking=False):
        return
    try:
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            visible = _albums_with_visible_tracks(db)
            to_warm = [info for key, info in _resolve_cover_info(db).items() if key in visible]
        finally:
            db.close()

        for info in to_warm:
            already_cached = asset_cache.cached_path("amazon-music-album", info["url"]) is not None
            try:
                asset_cache.get_or_fetch("amazon-music-album", info["url"])
            except Exception:
                # One album's unexpected failure shouldn't abort warm-up for
                # every album after it — logged and moved past, not reraised.
                logger.exception("amazon-music: cover warm-up failed for one album")
            if not already_cached:
                time.sleep(_WARM_UP_PACING_SECONDS)
    finally:
        _warm_up_lock.release()


@router.post(
    "/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "amazon-music-refresh")), Depends(require_csrf)],
)
def refresh_amazon_music(
    request: Request,
    background_tasks: BackgroundTasks,
    q: str = Form(""),
    sort: str = Form("added"),
    db: Session = Depends(get_db),
):
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

    if result is not None:
        background_tasks.add_task(_warm_album_covers)

    context = _albums_context(db, q.strip(), sort)
    context["refresh_result"] = result
    context["refresh_error"] = error
    return templates.TemplateResponse(request, "amazon_music/_albums_table.html", context)


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
    album_key: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """A checked-boxes selection queues just those; an empty selection queues
    every track the *current* filter/search (and album scope, on the album
    detail page) shows — mirrors bundles.py's own trigger_download (`items or
    None` means "no filter, download all"), which is also why "Download all"
    is the same form/button clearing its checkboxes on click rather than a
    separate action.
    """
    if download_id:
        await downloader.queue_many(db, download_id)
    else:
        query = db.query(AmazonMusicTrack.download_id)
        if album_key:
            query = query.filter(_album_asin_filter(album_key))
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
    return templates.TemplateResponse(
        request, "amazon_music/_table.html", _context(db, q.strip(), sort, show_missing, album_key)
    )


@router.get("/downloads/active", response_class=HTMLResponse)
def active_downloads(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "amazon_music/_active_downloads.html", {"active_downloads": downloader.get_active_downloads(db)}
    )


@router.post("/albums/downloads", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def trigger_album_downloads(
    request: Request,
    album_key: list[str] = Form(default=[]),
    q: str = Form(""),
    sort: str = Form("added"),
    show_missing: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Same "checked selection, or everything currently shown" convention as
    trigger_downloads above, just expanding each selected album into its own
    tracks first."""
    query = db.query(AmazonMusicTrack.download_id)
    if album_key:
        query = query.filter(or_(*[_album_asin_filter(k) for k in album_key]))
    elif q:
        like = f"%{q}%"
        query = query.filter(or_(AmazonMusicTrack.album.ilike(like), AmazonMusicTrack.artist.ilike(like)))
    if not show_missing:
        query = query.filter(AmazonMusicTrack.missing_since.is_(None))
    await downloader.queue_many(db, [row[0] for row in query.all()])
    return templates.TemplateResponse(
        request, "amazon_music/_albums_table.html", _albums_context(db, q.strip(), sort, show_missing)
    )


@router.post(
    "/albums/refresh-covers",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_cover_limiter, "amazon-music-refresh-cover")), Depends(require_csrf)],
)
def refresh_selected_covers(
    request: Request,
    album_key: list[str] = Form(default=[]),
    q: str = Form(""),
    sort: str = Form("added"),
    show_missing: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Bulk version of refresh_album_cover, for after something like a
    library-wide art issue rather than one album at a time — unlike the
    download bulk actions above, an empty selection here is a deliberate
    no-op rather than "refresh everything currently shown": forcing a
    refetch of every visible cover is a much heavier, more surprising action
    to make one accidental click away than queuing downloads is.
    """
    info_by_key = _resolve_cover_info(db)
    for key in album_key:
        info = info_by_key.get(key)
        if info:
            asset_cache.evict("amazon-music-album", info["url"], reason="bulk refresh requested")
    return templates.TemplateResponse(
        request, "amazon_music/_albums_table.html", _albums_context(db, q.strip(), sort, show_missing)
    )


@router.post("/albums/{album_key}/download", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def download_album(
    request: Request,
    album_key: str,
    show_missing: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Queues one album's tracks and re-renders just that row in place — this
    is what the grid's own per-row Download button targets (hx-target=
    "closest tr"), specifically so it doesn't reset a scrolled-through
    infinite-scroll session back to page 1 the way re-rendering the whole
    grid would. The bulk "Download selected"/"Download all" actions below
    still re-render the whole grid, since multiple rows' state changes at
    once there.
    """
    query = db.query(AmazonMusicTrack.download_id).filter(_album_asin_filter(album_key))
    if not show_missing:
        query = query.filter(AmazonMusicTrack.missing_since.is_(None))
    await downloader.queue_many(db, [row[0] for row in query.all()])
    row = _single_album_row(db, album_key)
    return templates.TemplateResponse(request, "amazon_music/_albums_row.html", {"a": row})


@router.post("/albums/{album_key}/sync-track-order", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def sync_album_track_order_route(
    request: Request,
    album_key: str,
    q: str = Form(""),
    sort: str = Form("added"),
    show_missing: bool = Form(False),
    db: Session = Depends(get_db),
):
    error = None
    if album_key == _NO_ALBUM_KEY:
        # No single catalog album to look up for a grouping of tracks that
        # each lack their own album deep link — nothing sane to fetch.
        error = "This group isn't a single Amazon album, so there's no track order to fetch for it."
    else:
        try:
            amazon_music_sync.sync_album_track_order(db, album_key)
        except amazon_music_sync.NotConnectedError as exc:
            error = str(exc)
        except AmazonMusicAuthError as exc:
            error = str(exc)
        except amazon_music_sync.AmazonMusicRequestError as exc:
            error = str(exc)
        except amazon_music_sync.NoTemplateError as exc:
            error = str(exc)
        except Exception as exc:
            error = f"The track-order fetch failed ({type(exc).__name__}). Amazon may have changed this page."

    context = _context(db, q.strip(), sort, show_missing, album_key)
    context["track_order_error"] = error
    return templates.TemplateResponse(request, "amazon_music/_table.html", context)


# Declared after /downloads/active on purpose: Starlette matches routes in
# declaration order, not by specificity, so this dynamic segment would
# otherwise shadow the literal /downloads/active route above it.
@router.post("/downloads/{download_id}", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def download_track(
    request: Request,
    download_id: str,
    q: str = Form(""),
    sort: str = Form("added"),
    album_key: str = Form(default=""),
    db: Session = Depends(get_db),
):
    try:
        await downloader.start_download(db, download_id)
    except downloader.UnknownTrackError:
        # A stale row from before a re-sync — nothing sane to do but ignore
        # the click; the table this renders won't offer that button once it
        # no longer lists the row.
        pass
    return templates.TemplateResponse(request, "amazon_music/_table.html", _context(db, q.strip(), sort, album_key=album_key))
