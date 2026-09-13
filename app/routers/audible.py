from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.models.audible_book import AudibleBook
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
    }


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
