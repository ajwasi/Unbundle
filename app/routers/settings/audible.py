from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import audible_connector
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_AUDIBLE, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.sync import audible_sync
from app.templates_env import templates

router = APIRouter()


def _get_or_create_audible_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_AUDIBLE)


def audible_context(db: Session, error: str | None = None) -> dict:
    """audible_pending drives which of three states _audible_modal_content.html
    renders: not connected (plain locale form), a login in progress waiting
    on the pasted-back redirect URL (audible_pending set, see
    audible_connector.login_status), or connected/error (the normal
    status+disconnect view every other connector card uses).
    """
    cred = Credential.get(db, SOURCE_AUDIBLE)
    return {
        "audible_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "audible_error": error if error is not None else (cred.last_error if cred else None),
        "audible_pending": audible_connector.login_status(),
    }


def _audible_response(
    request: Request,
    db: Session,
    error: str | None = None,
    just_connected: bool = False,
    connected_as: str | None = None,
):
    """Every Audible settings route (login start/answer/status, disconnect)
    renders this same response: an out-of-band update to the card behind the
    modal (status badge, Connect/Disconnect buttons) plus the modal's own
    content (login form, a pending CAPTCHA/OTP/CVF/approval prompt, or a
    "Connected" confirmation) — see _audible_response.html. One shared
    response shape means every action stays consistent regardless of which
    of the two ever triggered it.
    """
    context = audible_context(db, error)
    context["just_connected"] = just_connected
    context["audible_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_audible_response.html", context)


@router.post("/audible/login", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def start_audible_login(
    request: Request,
    locale: str = Form("us"),
    db: Session = Depends(get_db),
):
    locale = locale.strip().lower() or "us"

    error = None
    try:
        audible_connector.start_login(locale)
    except audible_connector.AudibleLoginError as exc:
        error = str(exc)

    return _audible_response(request, db, error)


@router.get("/audible/login/status", response_class=HTMLResponse)
def audible_login_status(request: Request, db: Session = Depends(get_db)):
    """Polled every 2s by _audible_modal_content.html while a login is in
    progress — same "poll a running background job" pattern
    bundles/_refresh_status.html already uses. Once login_result() has an
    answer, persists it (success or failure) and clears the pending state so
    this only ever fires once per login attempt.
    """
    just_connected = False
    connected_as = None
    result = audible_connector.login_result()
    if result is not None:
        auth, error = result
        if auth is not None:
            audible_sync.save_authenticator(db, auth)
            cred = Credential.get(db, SOURCE_AUDIBLE)
            cred.status = STATUS_OK
            cred.last_error = None
            just_connected = True
            # Best-effort: Amazon's device-registration response includes a
            # "customer_info" extension (confirmed present as a real
            # Authenticator attribute, requested by audible/register.py) —
            # its exact fields are unconfirmed against a real account from
            # this sandbox, so this degrades to no identity shown at all
            # rather than guessing further if it's ever absent/differently
            # shaped, the same philosophy audible_connector.py's own
            # best-effort field parsing already uses.
            customer_info = getattr(auth, "customer_info", None) or {}
            connected_as = customer_info.get("name") or customer_info.get("user_id")
        else:
            cred = _get_or_create_audible_credential(db)
            cred.status = STATUS_ERROR
            cred.last_error = error
        db.commit()
        audible_connector.clear_pending()

    return _audible_response(request, db, just_connected=just_connected, connected_as=connected_as)


@router.post("/audible/login/answer", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def answer_audible_login(request: Request, pasted_url: str = Form(""), db: Session = Depends(get_db)):
    audible_connector.answer_login(pasted_url.strip())
    return _audible_response(request, db)


@router.post("/audible/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_audible(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_AUDIBLE)
    if cred:
        db.delete(cred)
        db.commit()
    audible_connector.clear_pending()
    return _audible_response(request, db)
