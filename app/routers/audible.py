from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audible import pdf_downloader
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
from app.models.audible_book import AudibleBook
from app.models.audible_pdf_download import STATUS_COMPLETED, STATUS_QUEUED, STATUS_RUNNING
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


def _context(
    db: Session,
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
    return {
        "audible_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "audible_error": cred.last_error if cred else None,
        "books": books,
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


def _detail_context(db: Session, book: AudibleBook) -> dict:
    context = {"book": book, "owned": is_owned(book.benefit_id)}
    context.update(_pdf_status_context(db, book.asin))
    return context


@router.get("", response_class=HTMLResponse)
def audible_page(
    request: Request,
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
async def refresh_audible(request: Request, db: Session = Depends(get_db)):
    try:
        await audible_sync.refresh_audible_library(db)
    except audible_sync.NotConnectedError:
        pass
    except Exception:
        pass  # error already recorded on the credential row by refresh_audible_library
    return templates.TemplateResponse(request, "audible/_content.html", _context(db))


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
