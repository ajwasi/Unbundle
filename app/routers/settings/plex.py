from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import plex_connector
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_PLEX, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json
from app.sync import plex_sync
from app.templates_env import templates

router = APIRouter()


def _get_or_create_plex_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_PLEX)


def plex_context(db: Session, error: str | None = None) -> dict:
    cred = Credential.get(db, SOURCE_PLEX)
    payload = decrypt_json(cred.encrypted_payload) if cred and cred.encrypted_payload else {}
    return {
        "plex_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "plex_error": error if error is not None else (cred.last_error if cred else None),
        "plex_base_url": payload.get("base_url", ""),
        "plex_library_id": payload.get("library_id", ""),
        "plex_has_token": bool(payload.get("token")),
    }


@router.post("/plex", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_plex(
    request: Request,
    base_url: str = Form(""),
    token: str = Form(""),
    library_id: str = Form(""),
    db: Session = Depends(get_db),
):
    base_url = base_url.strip()
    token = token.strip()
    library_id = library_id.strip()
    cred = _get_or_create_plex_credential(db)
    existing = decrypt_json(cred.encrypted_payload) if cred.encrypted_payload else {}
    effective_url = base_url or existing.get("base_url", "")
    effective_token = token or existing.get("token", "")

    error = None
    connected_as = None
    if not effective_url or not effective_token:
        error = "Server URL and token are both required."
    else:
        result = await plex_connector.check_credentials(effective_url, effective_token)
        if result.ok:
            connected_as = result.message
        else:
            error = result.message

    cred.encrypted_payload = encrypt_json({"base_url": effective_url, "token": effective_token, "library_id": library_id})
    cred.status = STATUS_ERROR if error else STATUS_OK
    cred.last_error = error
    db.commit()

    context = plex_context(db)
    context["plex_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_plex_form.html", context)


@router.post("/plex/refresh", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def refresh_plex(request: Request, db: Session = Depends(get_db)):
    error = None
    count = None
    try:
        count = await plex_sync.refresh_plex_library(db)
    except Exception as exc:
        error = str(exc)

    context = plex_context(db, error=error)
    context["plex_refreshed_count"] = count
    return templates.TemplateResponse(request, "settings/_plex_form.html", context)


@router.post("/plex/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_plex(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_PLEX)
    if cred:
        db.delete(cred)
        db.commit()
    return templates.TemplateResponse(request, "settings/_plex_form.html", plex_context(db))
