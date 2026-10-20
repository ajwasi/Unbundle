from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audible import book_downloader, pdf_downloader
from app.connectors.audible_connector import is_owned
from app.csrf import require_csrf
from app.deps import get_db
from app.list_views import (
    apply_range_filter,
    parse_optional_date,
    parse_optional_float,
    sorted_query,
)
from app.models.audible_author_book import AudibleAuthorBook
from app.models.audible_author_profile import AudibleAuthorProfile
from app.models.audible_book import AudibleBook
from app.models.audible_book_download import STATUS_COMPLETED as BOOK_STATUS_COMPLETED
from app.models.audible_book_download import STATUS_FAILED as BOOK_STATUS_FAILED
from app.models.audible_book_download import STATUS_QUEUED as BOOK_STATUS_QUEUED
from app.models.audible_book_download import STATUS_RUNNING as BOOK_STATUS_RUNNING
from app.models.audible_download_destination import AudibleDownloadDestination
from app.models.audible_local_file import AudibleLocalFile
from app.models.audible_pdf_download import STATUS_COMPLETED, STATUS_QUEUED, STATUS_RUNNING
from app.models.audible_series_book import AudibleSeriesBook
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_AUDIBLE
from app.ratelimit import RateLimiter, rate_limit
from app.routers.destination_crud import destinations_context, register_simple_destination_routes
from app.sync import audible_sync, audiobookshelf_sync
from app.templates_env import templates

router = APIRouter(prefix="/audible")
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

_DEST_KWARGS = dict(
    url_prefix="/audible",
    dom_id="audible-destinations",
    folder_label="Audible",
    name_placeholder="NAS Audiobooks Share",
    path_placeholder="/mnt/library/audiobooks",
)
register_simple_destination_routes(router, AudibleDownloadDestination, **_DEST_KWARGS)

_SORT_COLUMNS = {
    "title": AudibleBook.title,
    "author": AudibleBook.author,
    "runtime": AudibleBook.runtime_minutes,
    "purchased": AudibleBook.purchase_date,
    "price": AudibleBook.price_amount,
    "rating": AudibleBook.rating_average,
    "progress": AudibleBook.percent_complete,
}


def _destinations_context(db: Session) -> dict:
    return destinations_context(db, AudibleDownloadDestination, **_DEST_KWARGS)


def _series_sort_key(sequence: str) -> tuple:
    try:
        return (0, float(sequence))
    except (TypeError, ValueError):
        return (1, sequence or "")


def _group_missing_by_series(rows: list[AudibleSeriesBook]) -> list[dict]:
    """rows must already be sorted by series_title, then sequence (the query
    below does this) — groups are then emitted in that same series_title
    order, and each group's own books list is sorted by sequence to match
    series_detail.html's own ordering. The group's cover_url is just the
    first book encountered with one, there being no dedicated series-cover
    field on AudibleSeriesBook (see that model's own docstring).
    """
    groups: dict[str, dict] = {}
    for row in rows:
        g = groups.get(row.series_asin)
        if g is None:
            g = groups[row.series_asin] = {
                "series_asin": row.series_asin,
                "series_title": row.series_title,
                "cover_url": "",
                "books": [],
            }
        if not g["cover_url"] and row.cover_url:
            g["cover_url"] = row.cover_url
        g["books"].append(row)
    for g in groups.values():
        g["books"].sort(key=lambda r: _series_sort_key(r.sequence))
    return list(groups.values())


def _group_missing_by_author(rows: list[AudibleAuthorBook]) -> list[dict]:
    """rows must already be sorted by author_name (the query below does
    this); each group's books are newest release first, then by title.
    """
    groups: dict[str, dict] = {}
    for row in rows:
        g = groups.get(row.author_name)
        if g is None:
            g = groups[row.author_name] = {
                "author_name": row.author_name,
                "author_asin": row.author_asin,
                "author_image_url": "",
                "books": [],
            }
        if not g["author_asin"] and row.author_asin:
            g["author_asin"] = row.author_asin
        g["books"].append(row)
    for g in groups.values():
        g["books"].sort(key=lambda r: r.title)
        g["books"].sort(key=lambda r: r.release_date, reverse=True)
    return list(groups.values())


def _context(
    db: Session,
    view: str = "owned",
    title: str = "",
    author: str = "",
    narrator: str = "",
    series: str = "",
    runtime_min: str = "",
    runtime_max: str = "",
    purchased_from: str = "",
    purchased_to: str = "",
    price_min: str = "",
    price_max: str = "",
    rating_min: str = "",
    rating_max: str = "",
    owned: str = "",
    progress: str = "",
    sort: str = "title",
    dir: str = "asc",
    series_q: str = "",
    author_q: str = "",
) -> dict:
    cred = Credential.get(db, SOURCE_AUDIBLE)
    query = db.query(AudibleBook)
    if title:
        query = query.filter(AudibleBook.title.ilike(f"%{title}%"))
    if author:
        query = query.filter(AudibleBook.author.ilike(f"%{author}%"))
    if narrator:
        query = query.filter(AudibleBook.narrator.ilike(f"%{narrator}%"))
    if series:
        query = query.filter(AudibleBook.series_title.ilike(f"%{series}%"))

    # Displayed/filtered in hours, stored in minutes.
    runtime_min_hours = parse_optional_float(runtime_min)
    runtime_max_hours = parse_optional_float(runtime_max)
    query = apply_range_filter(
        query,
        AudibleBook.runtime_minutes,
        runtime_min_hours * 60 if runtime_min_hours is not None else None,
        runtime_max_hours * 60 if runtime_max_hours is not None else None,
    )

    purchased_from_date = parse_optional_date(purchased_from)
    purchased_to_date = parse_optional_date(purchased_to)
    query = apply_range_filter(query, AudibleBook.purchase_date, purchased_from_date, purchased_to_date)

    price_min_val = parse_optional_float(price_min)
    price_max_val = parse_optional_float(price_max)
    query = apply_range_filter(query, AudibleBook.price_amount, price_min_val, price_max_val)

    rating_min_val = parse_optional_float(rating_min)
    rating_max_val = parse_optional_float(rating_max)
    query = apply_range_filter(query, AudibleBook.rating_average, rating_min_val, rating_max_val)

    if progress == "finished":
        query = query.filter(AudibleBook.is_finished.is_(True))
    elif progress == "in_progress":
        query = query.filter(AudibleBook.is_finished.is_(False), AudibleBook.percent_complete > 0)
    elif progress == "not_started":
        query = query.filter(AudibleBook.is_finished.is_(False), AudibleBook.percent_complete == 0)

    books = sorted_query(query, _SORT_COLUMNS, sort, dir, AudibleBook.title).all()
    # owned/Plus-Catalog is a plain Python membership check (is_owned(), on a
    # private module constant not worth importing into this router) applied
    # to the already-fetched, single-account-sized list — see the same
    # reasoning on is_owned() itself for why this isn't done in SQL.
    if owned == "owned":
        books = [b for b in books if is_owned(b.benefit_id)]
    elif owned == "plus":
        books = [b for b in books if not is_owned(b.benefit_id)]

    # Library-wide total, independent of the current filters — same "stable
    # overview vs. filtered table" split bundles/list.html's
    # grand_total_spent (vs. the table's own filtered_total_spent) already uses.
    total_book_count, total_runtime_minutes = db.query(
        func.count(AudibleBook.asin), func.coalesce(func.sum(AudibleBook.runtime_minutes), 0)
    ).one()

    # Computed against the current library at read time (not just at sync
    # time), so a book bought since the last series refresh drops off the
    # list immediately rather than waiting for the next refresh to catch up.
    owned_asins = {a for (a,) in db.query(AudibleBook.asin).all()}
    missing_rows = [
        row
        for row in db.query(AudibleSeriesBook)
        .order_by(AudibleSeriesBook.series_title, AudibleSeriesBook.sequence)
        .all()
        if row.asin not in owned_asins
    ]

    # Same read-time owned filter as missing_rows above, for the same reason.
    author_missing_rows = [
        row
        for row in db.query(AudibleAuthorBook).order_by(AudibleAuthorBook.author_name).all()
        if row.asin not in owned_asins
    ]
    missing_author_groups = _group_missing_by_author(author_missing_rows)
    missing_series_groups = _group_missing_by_series(missing_rows)

    # Thumbnail for the "Missing by author" listing — a separate lookup
    # rather than a join, since AudibleAuthorProfile is cached independently
    # (see its own docstring) and most groups share the same handful of
    # author_asins across many missing books.
    author_asins = {g["author_asin"] for g in missing_author_groups if g["author_asin"]}
    if author_asins:
        profiles_by_asin = {
            p.author_asin: p
            for p in db.query(AudibleAuthorProfile).filter(AudibleAuthorProfile.author_asin.in_(author_asins)).all()
        }
        for g in missing_author_groups:
            profile = profiles_by_asin.get(g["author_asin"])
            if profile is not None:
                g["author_image_url"] = profile.image_url
    # Captured before the search filter below (which reassigns these same
    # names) so the tab badges stay the stable, library-wide counts — same
    # "unaffected by the current filter" convention book_count already uses
    # for the Owned tab.
    missing_series_count = len(missing_series_groups)
    missing_author_count = len(missing_author_groups)

    # Search filters the GROUPS by name — whole series/author, not individual
    # books within one — matching what the search box sits next to.
    series_q = series_q.strip()
    if series_q:
        missing_series_groups = [g for g in missing_series_groups if series_q.casefold() in g["series_title"].casefold()]
    author_q = author_q.strip()
    if author_q:
        missing_author_groups = [g for g in missing_author_groups if author_q.casefold() in g["author_name"].casefold()]

    downloaded_asins = {a for (a,) in db.query(AudibleLocalFile.asin).all()}
    active_asins = {job["asin"] for job in book_downloader.get_active_jobs(db)}
    in_audiobookshelf_asins = audiobookshelf_sync.owned_asins(db)

    return {
        "audible_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "audible_error": cred.last_error if cred else None,
        "books": books,
        "downloaded_asins": downloaded_asins,
        "active_asins": active_asins,
        "in_audiobookshelf_asins": in_audiobookshelf_asins,
        # Each tab's badge shows "groups/books" — distinct series or authors,
        # then the total missing books across all of them — computed before
        # the search filter above, so it's the stable library-wide total,
        # same "book_count isn't affected by the Owned tab's own filters"
        # convention.
        "missing_count": len(missing_rows),
        "missing_series_count": missing_series_count,
        "missing_series_groups": missing_series_groups,
        "series_q": series_q,
        "missing_author_count": missing_author_count,
        "missing_author_book_count": len(author_missing_rows),
        "missing_author_groups": missing_author_groups,
        "author_q": author_q,
        **_destinations_context(db),
        "view": view if view in ("missing", "missing_authors") else "owned",
        "title": title,
        "author": author,
        "narrator": narrator,
        "series": series,
        "runtime_min": runtime_min_hours,
        "runtime_max": runtime_max_hours,
        "purchased_from": purchased_from_date,
        "purchased_to": purchased_to_date,
        "price_min": price_min_val,
        "price_max": price_max_val,
        "rating_min": rating_min_val,
        "rating_max": rating_max_val,
        "owned": owned,
        "progress": progress,
        "sort": sort,
        "dir": dir,
        "book_count": total_book_count,
        "total_runtime_hours": round(total_runtime_minutes / 60, 1),
        "last_synced": db.query(func.max(AudibleBook.fetched_at)).scalar(),
        "is_owned": is_owned,
    }


def _pdf_status_context(db: Session, asin: str) -> dict:
    job = pdf_downloader.latest_status(db, asin)
    return {
        "asin": asin,
        "pdf_job": job,
        "pdf_running": job is not None and job.status in (STATUS_QUEUED, STATUS_RUNNING),
        "pdf_completed": job is not None and job.status == STATUS_COMPLETED,
    }


def _book_status_context(db: Session, asin: str) -> dict:
    job = book_downloader.latest_status(db, asin)
    local = db.get(AudibleLocalFile, asin)
    failed = job is not None and job.status == BOOK_STATUS_FAILED
    return {
        "asin": asin,
        "book_job": job,
        "book_queued": job is not None and job.status == BOOK_STATUS_QUEUED,
        "book_running": job is not None and job.status in (BOOK_STATUS_QUEUED, BOOK_STATUS_RUNNING),
        # A file this app knows about counts as downloaded even with no job
        # for it — a folder scan records those. A newer failed attempt wins,
        # so its error stays visible.
        "book_completed": (job is not None and job.status == BOOK_STATUS_COMPLETED) or (local is not None and not failed),
        "book_local": local,
    }


def _detail_context(db: Session, book: AudibleBook) -> dict:
    context = {"book": book, "owned": is_owned(book.benefit_id)}
    context.update(_pdf_status_context(db, book.asin))
    context.update(_book_status_context(db, book.asin))
    return context


@router.get("", response_class=HTMLResponse)
def audible_page(
    request: Request,
    view: str = "owned",
    title: str = "",
    author: str = "",
    narrator: str = "",
    series: str = "",
    runtime_min: str = "",
    runtime_max: str = "",
    purchased_from: str = "",
    purchased_to: str = "",
    price_min: str = "",
    price_max: str = "",
    rating_min: str = "",
    rating_max: str = "",
    owned: str = "",
    progress: str = "",
    sort: str = "title",
    dir: str = "asc",
    series_q: str = "",
    author_q: str = "",
    db: Session = Depends(get_db),
):
    context = _context(
        db,
        view=view,
        title=title,
        author=author,
        narrator=narrator,
        series=series,
        runtime_min=runtime_min,
        runtime_max=runtime_max,
        purchased_from=purchased_from,
        purchased_to=purchased_to,
        price_min=price_min,
        price_max=price_max,
        rating_min=rating_min,
        rating_max=rating_max,
        owned=owned,
        progress=progress,
        sort=sort,
        dir=dir,
        series_q=series_q,
        author_q=author_q,
    )
    # Each tab's own search box re-requests just that tab's content, not the
    # owned-books table _books_table.html serves for every other htmx hit on
    # this route — same render_list_or_partial idea (full page on a plain
    # load, a scoped fragment on an htmx one), just view-dependent since
    # there are now three different fragments instead of one.
    if request.headers.get("HX-Request") == "true":
        if view == "missing":
            return templates.TemplateResponse(request, "audible/_missing_series.html", context)
        if view == "missing_authors":
            return templates.TemplateResponse(request, "audible/_missing_authors.html", context)
        return templates.TemplateResponse(request, "audible/_books_table.html", context)
    return templates.TemplateResponse(request, "audible/list.html", context)


@router.post(
    "/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "audible-refresh")), Depends(require_csrf)],
)
async def refresh_audible(
    request: Request,
    view: str = Form("owned"),
    series_q: str = Form(""),
    author_q: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        await audible_sync.refresh_audible_library(db)
    except audible_sync.NotConnectedError:
        pass
    except Exception:
        pass  # error already recorded on the credential row by refresh_audible_library
    return templates.TemplateResponse(
        request, "audible/_content.html", _context(db, view=view, series_q=series_q, author_q=author_q)
    )


@router.get("/series/{series_asin}", response_class=HTMLResponse)
def audible_series_detail(request: Request, series_asin: str, db: Session = Depends(get_db)):
    owned = db.query(AudibleBook).filter(AudibleBook.series_asin == series_asin).all()
    # Library-wide, not just this series' own owned rows: AudibleSeriesBook is
    # supposed to only ever hold still-unowned siblings (see its own
    # docstring), but a row goes stale the moment its book is bought after
    # the last series refresh — exactly like _context()'s own missing_rows
    # above, filtering at read time (not sync time) is what keeps a newly-
    # bought book from showing up as both Owned and Missing in the same table.
    owned_asins = {a for (a,) in db.query(AudibleBook.asin).all()}
    missing = [
        row
        for row in db.query(AudibleSeriesBook).filter(AudibleSeriesBook.series_asin == series_asin).all()
        if row.asin not in owned_asins
    ]
    if not owned and not missing:
        return templates.TemplateResponse(
            request, "audible/series_not_found.html", {"series_asin": series_asin}, status_code=404
        )
    series_title = (owned[0].series_title if owned else missing[0].series_title) or ""

    rows = [
        {
            "owned": True,
            "asin": b.asin,
            "title": b.title,
            "authors": b.author,
            "sequence": b.series_sequence,
            "cover_url": b.cover_url,
            "price": b.price_amount,
            "currency": b.price_currency,
            "discount_pct": None,
        }
        for b in owned
    ] + [
        {
            "owned": False,
            "asin": s.asin,
            "title": s.title,
            "authors": s.authors,
            "sequence": s.sequence,
            "cover_url": s.cover_url,
            "price": s.current_price,
            "currency": s.currency,
            "discount_pct": s.discount_pct,
        }
        for s in missing
    ]
    rows.sort(key=lambda r: _series_sort_key(r["sequence"]))

    return templates.TemplateResponse(
        request,
        "audible/series_detail.html",
        {"series_title": series_title, "series_asin": series_asin, "rows": rows},
    )


@router.get("/author/{author_name}", response_class=HTMLResponse)
def audible_author_detail(request: Request, author_name: str, db: Session = Depends(get_db)):
    # AudibleBook has no normalized per-author list, only the flattened
    # display string fetch_library() already joined (see its own docstring)
    # — split it back out and match the exact name, same reasoning
    # audible_sync._is_contributor_credit's caller already relies on
    # (author_name here only ever came from AudibleAuthorBook.author_name,
    # which only ever holds names that already passed that same filter, so
    # there's no contributor-credit noise to re-exclude here).
    owned = [
        b
        for b in db.query(AudibleBook).all()
        if author_name.casefold() in {n.strip().casefold() for n in (b.author or "").split(",")}
    ]
    missing = db.query(AudibleAuthorBook).filter(AudibleAuthorBook.author_name == author_name).all()
    # Captured before the owned-filter below (which can empty `missing`
    # entirely) — the author's own asin is the same regardless of which rows
    # survive that filter, and it's only ever known via a missing-book row
    # (see parse_author_book_item) — an author with everything already owned
    # has no row to find it on at all, so this page shows no photo/bio for
    # them rather than expanding the owned-books path to capture it too.
    author_asin = missing[0].author_asin if missing else ""
    # Same read-time owned filter as everywhere else in this file — a book
    # bought since the last refresh shouldn't show up as both.
    owned_asins = {b.asin for b in owned}
    missing = [m for m in missing if m.asin not in owned_asins]
    if not owned and not missing:
        return templates.TemplateResponse(
            request, "audible/author_not_found.html", {"author_name": author_name}, status_code=404
        )

    profile = db.get(AudibleAuthorProfile, author_asin) if author_asin else None

    rows = [
        {
            "owned": True,
            "asin": b.asin,
            "title": b.title,
            "series_title": b.series_title,
            "series_sequence": b.series_sequence,
            "cover_url": b.cover_url,
            "price": b.price_amount,
            "currency": b.price_currency,
            "discount_pct": None,
        }
        for b in owned
    ] + [
        {
            "owned": False,
            "asin": m.asin,
            "title": m.title,
            "series_title": m.series_title,
            "series_sequence": m.series_sequence,
            "cover_url": m.cover_url,
            "price": m.current_price,
            "currency": m.currency,
            "discount_pct": m.discount_pct,
        }
        for m in missing
    ]
    # No publication date is stored for owned books (fetch_library never
    # requests/parses one — AudibleBook only has purchase_date, which is
    # when the account bought it, not when Audible released it), so unlike
    # the tab's own missing-only groups (sorted newest-release-first), the
    # combined owned+missing list here sorts by title — the one ordering
    # that means the same thing for a row from either source.
    rows.sort(key=lambda r: r["title"].casefold())

    return templates.TemplateResponse(
        request,
        "audible/author_detail.html",
        {
            "author_name": author_name,
            "owned_count": len(owned),
            "missing_count": len(missing),
            "rows": rows,
            "author_image_url": profile.image_url if profile else "",
            "author_bio": profile.bio if profile else "",
        },
    )


@router.post("/download", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def start_bulk_download(request: Request, asins: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    # async def for the same reason start_book_download below is: the queue's
    # dispatch spawns background tasks, which need the running event loop.
    outcome = book_downloader.queue_many(db, asins)
    return templates.TemplateResponse(request, "audible/_bulk_result.html", {"outcome": outcome, "requested": len(asins)})


@router.get("/{asin}", response_class=HTMLResponse)
def audible_detail(request: Request, asin: str, db: Session = Depends(get_db)):
    book = db.get(AudibleBook, asin)
    if book is None:
        return templates.TemplateResponse(request, "audible/not_found.html", {"asin": asin}, status_code=404)
    return templates.TemplateResponse(request, "audible/detail.html", _detail_context(db, book))


@router.post("/{asin}/pdf/download", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def start_pdf_download(request: Request, asin: str, db: Session = Depends(get_db)):
    # Must run on the event loop, not FastAPI's sync-route threadpool —
    # pdf_downloader.start_download() calls asyncio.create_task(), which
    # requires a running loop in the calling thread (same reason
    # routers/bundles.py's trigger_download()/trigger_item_download() are
    # async def too).
    try:
        pdf_downloader.start_download(db, asin)
    except (pdf_downloader.NoPdfAvailableError, pdf_downloader.PdfDownloadInProgressError):
        pass  # surfaced via the same status partial either way
    return templates.TemplateResponse(request, "audible/_pdf_status.html", _pdf_status_context(db, asin))


@router.get("/{asin}/pdf/status", response_class=HTMLResponse)
def pdf_download_status(request: Request, asin: str, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "audible/_pdf_status.html", _pdf_status_context(db, asin))


@router.get("/{asin}/pdf/file")
def download_pdf_file(asin: str, db: Session = Depends(get_db)):
    job = pdf_downloader.latest_status(db, asin)
    if job is None or job.status != STATUS_COMPLETED or not job.downloaded_path:
        return HTMLResponse("No completed PDF download found for this title.", status_code=404)
    book = db.get(AudibleBook, asin)
    filename = f"{book.title}.pdf" if book else f"{asin}.pdf"
    return FileResponse(job.downloaded_path, filename=filename, media_type="application/pdf")


@router.post("/{asin}/book/download", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def start_book_download(request: Request, asin: str, force: bool = Form(False), db: Session = Depends(get_db)):
    # async def for the same reason start_pdf_download above is — must run on
    # the event loop so book_downloader.start_download()'s background task
    # has one to attach to. force=True (the "Re-download" button) bypasses
    # the already-downloaded-on-disk shortcut and always fetches again.
    try:
        book_downloader.start_download(db, asin, force=force)
    except (book_downloader.NoAudiobookAvailableError, book_downloader.BookDownloadInProgressError):
        pass  # surfaced via the same status partial either way
    return templates.TemplateResponse(request, "audible/_book_status.html", _book_status_context(db, asin))


@router.get("/{asin}/book/status", response_class=HTMLResponse)
def book_download_status(request: Request, asin: str, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "audible/_book_status.html", _book_status_context(db, asin))


@router.get("/{asin}/book/file")
def download_book_file(asin: str, db: Session = Depends(get_db)):
    job = book_downloader.latest_status(db, asin)
    path = job.downloaded_path if job is not None and job.status == BOOK_STATUS_COMPLETED else ""
    if not path:
        local = db.get(AudibleLocalFile, asin)
        path = local.path if local is not None else ""
    if not path or not Path(path).is_file():
        return HTMLResponse("No completed audiobook download found for this title.", status_code=404)
    book = db.get(AudibleBook, asin)
    filename = f"{book.title}.m4b" if book else f"{asin}.m4b"
    return FileResponse(path, filename=filename, media_type="audio/mp4")
