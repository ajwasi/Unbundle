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
    render_list_or_partial,
    sorted_query,
)
from app.models.audible_author_book import AudibleAuthorBook
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
from app.sync import audible_sync
from app.templates_env import templates

router = APIRouter(prefix="/audible")
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

_SORT_COLUMNS = {
    "title": AudibleBook.title,
    "author": AudibleBook.author,
    "runtime": AudibleBook.runtime_minutes,
    "purchased": AudibleBook.purchase_date,
    "price": AudibleBook.price_amount,
    "rating": AudibleBook.rating_average,
    "progress": AudibleBook.percent_complete,
}


def _destination_rows(db: Session) -> list[dict]:
    return [
        {"id": d.id, "name": d.name, "path": d.path, "is_default": d.is_default}
        for d in db.query(AudibleDownloadDestination).order_by(AudibleDownloadDestination.id).all()
    ]


def _destinations_context(db: Session) -> dict:
    return {"destinations": _destination_rows(db)}


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
            g = groups[row.author_name] = {"author_name": row.author_name, "books": []}
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

    downloaded_asins = {a for (a,) in db.query(AudibleLocalFile.asin).all()}
    active_asins = {job["asin"] for job in book_downloader.get_active_jobs(db)}

    return {
        "audible_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "audible_error": cred.last_error if cred else None,
        "books": books,
        "downloaded_asins": downloaded_asins,
        "active_asins": active_asins,
        "missing_count": len(missing_rows),
        "missing_series_groups": _group_missing_by_series(missing_rows),
        "missing_author_count": len(author_missing_rows),
        "missing_author_groups": _group_missing_by_author(author_missing_rows),
        "destinations": _destination_rows(db),
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
    )
    return render_list_or_partial(request, templates, "audible/list.html", "audible/_books_table.html", context)


@router.post(
    "/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_refresh_limiter, "audible-refresh")), Depends(require_csrf)],
)
async def refresh_audible(request: Request, view: str = Form("owned"), db: Session = Depends(get_db)):
    try:
        await audible_sync.refresh_audible_library(db)
    except audible_sync.NotConnectedError:
        pass
    except Exception:
        pass  # error already recorded on the credential row by refresh_audible_library
    return templates.TemplateResponse(request, "audible/_content.html", _context(db, view=view))


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


@router.post("/destinations", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def create_destination(request: Request, name: str = Form(default=""), path: str = Form(default=""), db: Session = Depends(get_db)):
    if name.strip() and path.strip():
        # The first destination anyone adds becomes the default automatically
        # — otherwise a user who configures exactly one directory would still
        # need a second click to actually use it.
        is_first = db.query(AudibleDownloadDestination).count() == 0
        db.add(AudibleDownloadDestination(name=name.strip(), path=path.strip(), is_default=is_first))
        db.commit()
    return templates.TemplateResponse(request, "audible/_destinations.html", _destinations_context(db))


@router.post("/destinations/{destination_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def delete_destination(request: Request, destination_id: int, db: Session = Depends(get_db)):
    dest = db.get(AudibleDownloadDestination, destination_id)
    if dest is not None:
        db.delete(dest)
        db.commit()
    return templates.TemplateResponse(request, "audible/_destinations.html", _destinations_context(db))


@router.post("/destinations/{destination_id}/set-default", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def set_default_destination(request: Request, destination_id: int, db: Session = Depends(get_db)):
    dest = db.get(AudibleDownloadDestination, destination_id)
    if dest is not None:
        db.query(AudibleDownloadDestination).update({"is_default": False})
        dest.is_default = True
        db.commit()
    return templates.TemplateResponse(request, "audible/_destinations.html", _destinations_context(db))
