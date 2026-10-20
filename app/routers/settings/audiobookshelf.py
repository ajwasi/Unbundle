from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import audiobookshelf_connector
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_AUDIOBOOKSHELF, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json
from app.sync import audiobookshelf_sync
from app.templates_env import templates

router = APIRouter()


def _get_or_create_audiobookshelf_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_AUDIOBOOKSHELF)


def audiobookshelf_context(db: Session, error: str | None = None) -> dict:
    cred = Credential.get(db, SOURCE_AUDIOBOOKSHELF)
    payload = decrypt_json(cred.encrypted_payload) if cred and cred.encrypted_payload else {}
    return {
        "audiobookshelf_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "audiobookshelf_error": error if error is not None else (cred.last_error if cred else None),
        "audiobookshelf_base_url": payload.get("base_url", ""),
        "audiobookshelf_library_id": payload.get("library_id", ""),
        "audiobookshelf_has_token": bool(payload.get("api_token")),
    }


@router.post("/audiobookshelf", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_audiobookshelf(
    request: Request,
    base_url: str = Form(""),
    api_token: str = Form(""),
    library_id: str = Form(""),
    db: Session = Depends(get_db),
):
    base_url = base_url.strip()
    api_token = api_token.strip()
    library_id = library_id.strip()
    cred = _get_or_create_audiobookshelf_credential(db)
    existing = decrypt_json(cred.encrypted_payload) if cred.encrypted_payload else {}
    effective_url = base_url or existing.get("base_url", "")
    effective_token = api_token or existing.get("api_token", "")

    error = None
    connected_as = None
    if not effective_url or not effective_token:
        error = "Server URL and API token are both required."
    else:
        result = await audiobookshelf_connector.check_credentials(effective_url, effective_token)
        if result.ok:
            connected_as = result.message
        else:
            error = result.message

    cred.encrypted_payload = encrypt_json({"base_url": effective_url, "api_token": effective_token, "library_id": library_id})
    cred.status = STATUS_ERROR if error else STATUS_OK
    cred.last_error = error
    db.commit()

    context = audiobookshelf_context(db)
    context["audiobookshelf_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_audiobookshelf_form.html", context)


@router.post("/audiobookshelf/refresh", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def refresh_audiobookshelf(request: Request, db: Session = Depends(get_db)):
    error = None
    count = None
    try:
        count = await audiobookshelf_sync.refresh_audiobookshelf_library(db)
    except Exception as exc:
        error = str(exc)

    context = audiobookshelf_context(db, error=error)
    context["audiobookshelf_refreshed_count"] = count
    return templates.TemplateResponse(request, "settings/_audiobookshelf_form.html", context)


@router.post("/audiobookshelf/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_audiobookshelf(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_AUDIOBOOKSHELF)
    if cred:
        db.delete(cred)
        db.commit()
    return templates.TemplateResponse(request, "settings/_audiobookshelf_form.html", audiobookshelf_context(db))
