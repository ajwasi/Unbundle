from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors import gog_connector
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_GOG, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.ratelimit import RateLimiter, rate_limit
from app.security import encrypt_json
from app.sync import gog_sync
from app.templates_env import templates

router = APIRouter()

# Same login-shaped-outbound-call ceiling as every other one in this app —
# this one validates a pasted GOG session cookie with a real request to gog.com.
_gog_cookie_limiter = RateLimiter(max_calls=5, period_seconds=60)


def _get_or_create_gog_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_GOG)


def gog_context(db: Session, error: str | None = None) -> dict:
    cred = Credential.get(db, SOURCE_GOG)
    payload = Credential.get_payload(db, SOURCE_GOG) or {}
    return {
        "gog_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "gog_error": error if error is not None else (cred.last_error if cred else None),
        "gog_login_url": gog_connector.LOGIN_URL,
        # Independent of gog_status/gog_error, which track the OAuth/library
        # connection only — the order-history cookie is a second, optional
        # credential on the same row (see gog_sync._save_gog_payload_field).
        "gog_cookie_configured": bool(payload.get("cookie")),
        "demo_mode": settings.demo_mode,
    }


def _gog_response(
    request: Request,
    db: Session,
    error: str | None = None,
    just_connected: bool = False,
    connected_as: str | None = None,
):
    """Every state-changing route for the OAuth library connection
    (save/disconnect) renders this same response: an out-of-band update to
    the card behind the modal (status badge, Connect/Disconnect) plus the
    modal's own content (paste-URL form, or a "Connected" confirmation) —
    mirrors audible._audible_response()'s identical shape (for the same
    reason: one response shape means every action stays consistent
    regardless of which of the two ever triggered it).

    Not used by the order-history cookie routes below — that credential has
    nothing to do with the OAuth modal, so those just re-render
    settings/_gog_form.html directly into #gog-card-body.
    """
    context = gog_context(db, error)
    context["just_connected"] = just_connected
    context["gog_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_gog_response.html", context)


@router.post("/gog", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_gog(request: Request, pasted_code: str = Form(""), db: Session = Depends(get_db)):
    code = gog_connector.extract_code(pasted_code)

    error = None
    connected_as = None
    if not code:
        error = "Paste the URL or code you were redirected to after logging in."
    else:
        try:
            tokens = await gog_connector.exchange_code(code)
        except gog_connector.GogAuthError as exc:
            error = str(exc)
        else:
            gog_sync.save_refresh_token(db, tokens["refresh_token"])
            # Best-effort: GOG's real token response is documented (by the
            # reverse-engineering community, not GOG itself — see
            # gog_connector.py's own caveat) to include a numeric account
            # identifier alongside the tokens. Shown once, right after
            # connecting, as confirmation of which account just got linked —
            # the same role Steam's own check_credentials() message already
            # plays — degrading to no identity shown at all if it's ever
            # absent (e.g. demo mode's mock response) rather than guessing.
            connected_as = str(tokens["user_id"]) if tokens.get("user_id") else None

    cred = _get_or_create_gog_credential(db)
    cred.status = STATUS_ERROR if error else STATUS_OK
    cred.last_error = error
    db.commit()

    return _gog_response(request, db, error, just_connected=not error, connected_as=connected_as)


@router.post("/gog/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_gog(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_GOG)
    if cred:
        db.delete(cred)
        db.commit()
    return _gog_response(request, db)


@router.post(
    "/gog/cookie",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_gog_cookie_limiter, "gog-cookie")), Depends(require_csrf)],
)
async def save_gog_cookie(request: Request, cookie: str = Form(...), db: Session = Depends(get_db)):
    # Unrelated to the OAuth modal flow above — a second, independent
    # credential on the same row (see gog_sync._save_gog_payload_field), so
    # this re-renders settings/_gog_form.html directly into #gog-card-body
    # rather than going through _gog_response/_gog_modal_content, which only
    # ever speaks to the OAuth connect flow.
    cookie = cookie.strip()
    cookie_error = None
    try:
        await gog_connector.fetch_order_history(cookie)
    except gog_connector.GogAuthError as exc:
        cookie_error = str(exc)
    else:
        gog_sync.save_cookie(db, cookie)
    context = gog_context(db)
    context["gog_cookie_error"] = cookie_error
    return templates.TemplateResponse(request, "settings/_gog_form.html", context)


@router.post("/gog/cookie/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_gog_cookie(request: Request, db: Session = Depends(get_db)):
    # Removes only the order-history cookie, keeping the OAuth refresh_token
    # (and the library connection it represents) intact — the two are
    # independent credentials on the same row (see
    # gog_sync._save_gog_payload_field), unlike the full /gog/disconnect
    # above, which deliberately clears both at once.
    payload = gog_sync.get_gog_credential(db) or {}
    if "cookie" in payload:
        payload.pop("cookie")
        cred = Credential.get(db, SOURCE_GOG)
        if payload:
            cred.encrypted_payload = encrypt_json(payload)
        else:
            db.delete(cred)
        db.commit()
    return templates.TemplateResponse(request, "settings/_gog_form.html", gog_context(db))
