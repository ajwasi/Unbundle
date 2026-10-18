import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import chirp_connector
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_CHIRP, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.ratelimit import RateLimiter, rate_limit
from app.security import encrypt_json
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter()

# Chirp sits behind Cloudflare (see chirp_connector.py's own docstring) —
# capped the same way every other login-shaped outbound call in this app is,
# rather than letting repeated clicks here hammer a bot-protected endpoint.
_chirp_login_limiter = RateLimiter(max_calls=5, period_seconds=60)


def chirp_context(db: Session, chirp_error: str | None = None) -> dict:
    cred = Credential.get(db, SOURCE_CHIRP)
    return {
        "chirp_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "chirp_error": chirp_error if chirp_error is not None else (cred.last_error if cred else None),
    }


@router.post(
    "/chirp",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_chirp_login_limiter, "chirp-login")), Depends(require_csrf)],
)
async def save_chirp(request: Request, cookie: str = Form(...), db: Session = Depends(get_db)):
    cookie = cookie.strip()
    try:
        result = await chirp_connector.verify_cookie_session(cookie)
    except Exception as exc:  # an undocumented, reverse-engineered API — a shape change is plausible
        logger.exception("chirp: verify_cookie_session raised an unexpected exception")
        result = chirp_connector.CredentialStatus(
            ok=False, message=f"Saving failed ({type(exc).__name__}). Chirp may have changed something."
        )
    cred = Credential.get_or_create(db, SOURCE_CHIRP)

    if result.ok:
        cred.encrypted_payload = encrypt_json({"cookie": cookie})
        cred.status = STATUS_OK
        cred.last_error = None
    else:
        cred.status = STATUS_ERROR
        cred.last_error = result.message
    db.commit()

    context = chirp_context(db)
    context["chirp_connected_as"] = result.message if result.ok else None
    return templates.TemplateResponse(request, "settings/_chirp_form.html", context)


@router.post("/chirp/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_chirp(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_CHIRP)
    if cred:
        db.delete(cred)
        db.commit()
    return templates.TemplateResponse(request, "settings/_chirp_form.html", chirp_context(db))
