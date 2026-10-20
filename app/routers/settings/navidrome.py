from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import navidrome_connector
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_NAVIDROME, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json
from app.sync import navidrome_sync
from app.templates_env import templates

router = APIRouter()


def _get_or_create_navidrome_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_NAVIDROME)


def navidrome_context(db: Session, error: str | None = None) -> dict:
    cred = Credential.get(db, SOURCE_NAVIDROME)
    payload = decrypt_json(cred.encrypted_payload) if cred and cred.encrypted_payload else {}
    return {
        "navidrome_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "navidrome_error": error if error is not None else (cred.last_error if cred else None),
        "navidrome_base_url": payload.get("base_url", ""),
        "navidrome_username": payload.get("username", ""),
        "navidrome_has_password": bool(payload.get("password")),
    }


@router.post("/navidrome", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_navidrome(
    request: Request,
    base_url: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    base_url = base_url.strip()
    username = username.strip()
    cred = _get_or_create_navidrome_credential(db)
    existing = decrypt_json(cred.encrypted_payload) if cred.encrypted_payload else {}
    effective_url = base_url or existing.get("base_url", "")
    effective_username = username or existing.get("username", "")
    effective_password = password or existing.get("password", "")

    error = None
    connected_as = None
    if not effective_url or not effective_username or not effective_password:
        error = "Server URL, username, and password are all required."
    else:
        result = await navidrome_connector.check_credentials(effective_url, effective_username, effective_password)
        if result.ok:
            connected_as = result.message
        else:
            error = result.message

    cred.encrypted_payload = encrypt_json({"base_url": effective_url, "username": effective_username, "password": effective_password})
    cred.status = STATUS_ERROR if error else STATUS_OK
    cred.last_error = error
    db.commit()

    context = navidrome_context(db)
    context["navidrome_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_navidrome_form.html", context)


@router.post("/navidrome/refresh", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def refresh_navidrome(request: Request, db: Session = Depends(get_db)):
    error = None
    count = None
    try:
        count = await navidrome_sync.refresh_navidrome_library(db)
    except Exception as exc:
        error = str(exc)

    context = navidrome_context(db, error=error)
    context["navidrome_refreshed_count"] = count
    return templates.TemplateResponse(request, "settings/_navidrome_form.html", context)


@router.post("/navidrome/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_navidrome(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_NAVIDROME)
    if cred:
        db.delete(cred)
        db.commit()
    return templates.TemplateResponse(request, "settings/_navidrome_form.html", navidrome_context(db))
