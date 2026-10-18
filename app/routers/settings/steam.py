from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import steam_connector
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_STEAM, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json
from app.templates_env import templates

router = APIRouter()


def _get_or_create_steam_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_STEAM)


def steam_context(db: Session, steam_error: str | None = None) -> dict:
    cred = Credential.get(db, SOURCE_STEAM)
    payload = decrypt_json(cred.encrypted_payload) if cred and cred.encrypted_payload else {}
    return {
        "steam_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "steam_error": steam_error if steam_error is not None else (cred.last_error if cred else None),
        "steam_has_key": bool(payload.get("api_key")),
        "steam_id_display": payload.get("steamid64", ""),
    }


@router.post("/steam", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_steam(request: Request, api_key: str = Form(""), steamid: str = Form(""), db: Session = Depends(get_db)):
    api_key = api_key.strip()
    steamid_input = steamid.strip()
    cred = _get_or_create_steam_credential(db)
    existing = decrypt_json(cred.encrypted_payload) if cred.encrypted_payload else {}
    effective_key = api_key or existing.get("api_key", "")
    effective_steamid_input = steamid_input or existing.get("steamid64", "")

    error = None
    connected_as = None
    resolved_steamid = existing.get("steamid64", "")
    if not effective_key or not effective_steamid_input:
        error = "API Key and SteamID (or profile URL) are both required."
    else:
        try:
            resolved_steamid = await steam_connector.resolve_steamid(effective_key, effective_steamid_input)
            result = await steam_connector.check_credentials(effective_key, resolved_steamid)
            if result.ok:
                connected_as = result.message
            else:
                error = result.message
        except Exception as exc:
            error = str(exc)

    cred.encrypted_payload = encrypt_json({"api_key": effective_key, "steamid64": resolved_steamid})
    cred.status = STATUS_ERROR if error else STATUS_OK
    cred.last_error = error
    db.commit()

    context = steam_context(db)
    context["steam_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_steam_form.html", context)


@router.post("/steam/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_steam(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_STEAM)
    if cred:
        db.delete(cred)
        db.commit()
    return templates.TemplateResponse(request, "settings/_steam_form.html", steam_context(db))
