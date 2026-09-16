from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.audible import pdf_downloader
from app.connectors.audible_connector import is_owned
from app.csrf import require_csrf
from app.deps import get_db
from app.list_views import render_list_or_partial, sorted_query
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
}


def _context(db: Session, q: str = "", owned: str = "", sort: str = "title", dir: str = "asc") -> dict:
    cred = Credential.get(db, SOURCE_AUDIBLE)
    query = db.query(AudibleBook)
    if q:
        query = query.filter(or_(AudibleBook.title.ilike(f"%{q}%"), AudibleBook.author.ilike(f"%{q}%")))
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
        "q": q,
        "owned": owned,
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
    q: str = "",
    owned: str = "",
    sort: str = "title",
    dir: str = "asc",
    db: Session = Depends(get_db),
):
    context = _context(db, q, owned, sort, dir)
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
