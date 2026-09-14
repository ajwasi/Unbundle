from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audible import pdf_downloader
from app.connectors.audible_connector import is_owned
from app.csrf import require_csrf
from app.deps import get_db
from app.models.audible_book import AudibleBook
from app.models.audible_pdf_download import STATUS_COMPLETED, STATUS_QUEUED, STATUS_RUNNING
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_AUDIBLE
from app.ratelimit import RateLimiter, rate_limit
from app.sync import audible_sync
from app.templates_env import templates

router = APIRouter(prefix="/audible")
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)


def _context(db: Session) -> dict:
    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one_or_none()
    books = db.query(AudibleBook).order_by(AudibleBook.title).all()
    total_runtime_hours = round(sum(b.runtime_minutes for b in books) / 60, 1)
    return {
        "audible_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "audible_error": cred.last_error if cred else None,
        "books": books,
        "book_count": len(books),
        "total_runtime_hours": total_runtime_hours,
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
def audible_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "audible/list.html", _context(db))


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
