"""Chirp Books — a read-only preview of the purchased library, built
entirely on top of app/connectors/chirp_connector.py (see its own docstring
for what's confirmed vs. reconstructed vs. still unknown about that API,
including why this uses a pasted browser cookie rather than a stored
email/password — a plain server-side login is confirmed blocked by
Cloudflare).

No sync, no persisted model, no download yet: "Check library" below
performs the same live cookie-session fetch as Settings' own "Save" button,
just reachable from this app's own UI, and showing the actual book list
rather than only a pass/fail message.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import chirp_connector as chirp
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_CHIRP, STATUS_NOT_CONFIGURED, Credential
from app.ratelimit import RateLimiter, rate_limit
from app.security import decrypt_json
from app.templates_env import templates

router = APIRouter(prefix="/chirp")

# Same ceiling as Settings' own chirp-login limiter — this button performs
# the identical live login against a Cloudflare-protected endpoint.
_check_limiter = RateLimiter(max_calls=5, period_seconds=60)


def _stored_cookie(db: Session) -> str | None:
    cred = Credential.get(db, SOURCE_CHIRP)
    if cred is None or not cred.encrypted_payload:
        return None
    cookie = decrypt_json(cred.encrypted_payload).get("cookie", "")
    return cookie or None


def _context(db: Session) -> dict:
    cred = Credential.get(db, SOURCE_CHIRP)
    return {
        "chirp_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "books": None,
        "total": None,
        "check_error": None,
    }


@router.get("", response_class=HTMLResponse)
def chirp_page(request: Request, db: Session = Depends(get_db)):
    template = "chirp/_content.html" if request.headers.get("HX-Request") else "chirp/index.html"
    return templates.TemplateResponse(request, template, _context(db))


@router.post(
    "/check",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_check_limiter, "chirp-check")), Depends(require_csrf)],
)
async def check_library(request: Request, db: Session = Depends(get_db)):
    context = _context(db)
    cookie = _stored_cookie(db)
    if cookie is None:
        context["check_error"] = "Chirp isn't connected yet — paste your Cookie header value in Settings first."
        return templates.TemplateResponse(request, "chirp/_content.html", context)

    try:
        books, total = await chirp.fetch_library_preview_via_cookie(cookie, page=1)
    except UnicodeEncodeError as exc:
        context["check_error"] = chirp.describe_cookie_unicode_error(cookie, exc)
    except chirp.ChirpRequestError as exc:
        context["check_error"] = str(exc)
    except Exception as exc:  # an undocumented, reverse-engineered API — a shape change is plausible
        context["check_error"] = f"The check failed ({type(exc).__name__}). Chirp may have changed something."
    else:
        context["books"] = books
        context["total"] = total

    return templates.TemplateResponse(request, "chirp/_content.html", context)
