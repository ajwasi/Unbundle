import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors import gog_connector, steam_connector
from app.connectors.humble_connector import HumbleConnector
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import (
    SOURCE_GOG,
    SOURCE_HUMBLE,
    SOURCE_OIDC,
    SOURCE_STEAM,
    STATUS_ERROR,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    Credential,
)
from app.oidc import discover, get_oidc_config
from app.security import decrypt_json, encrypt_json
from app.sync import gog_sync
from app.templates_env import templates

router = APIRouter(prefix="/settings")


def _humble_credential(db: Session) -> Credential | None:
    return db.query(Credential).filter(Credential.source == SOURCE_HUMBLE).one_or_none()


def _get_or_create_humble_credential(db: Session) -> Credential:
    cred = _humble_credential(db)
    if cred is None:
        cred = Credential(source=SOURCE_HUMBLE)
        db.add(cred)
    return cred


def _get_or_create_oidc_credential(db: Session) -> Credential:
    cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one_or_none()
    if cred is None:
        cred = Credential(source=SOURCE_OIDC)
        db.add(cred)
    return cred


def _get_or_create_steam_credential(db: Session) -> Credential:
    cred = db.query(Credential).filter(Credential.source == SOURCE_STEAM).one_or_none()
    if cred is None:
        cred = Credential(source=SOURCE_STEAM)
        db.add(cred)
    return cred


def _steam_context(db: Session, steam_error: str | None = None) -> dict:
    cred = db.query(Credential).filter(Credential.source == SOURCE_STEAM).one_or_none()
    payload = decrypt_json(cred.encrypted_payload) if cred and cred.encrypted_payload else {}
    return {
        "steam_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "steam_error": steam_error if steam_error is not None else (cred.last_error if cred else None),
        "steam_has_key": bool(payload.get("api_key")),
        "steam_id_display": payload.get("steamid64", ""),
    }


def _get_or_create_gog_credential(db: Session) -> Credential:
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one_or_none()
    if cred is None:
        cred = Credential(source=SOURCE_GOG)
        db.add(cred)
    return cred


def _gog_context(db: Session) -> dict:
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one_or_none()
    return {
        "gog_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "gog_error": cred.last_error if cred else None,
        "gog_login_url": gog_connector.LOGIN_URL,
    }


def _oidc_context(request: Request, db: Session, oidc_error: str | None = None) -> dict:
    cfg = get_oidc_config(db) or {}
    return {
        "oidc_issuer": cfg.get("issuer", ""),
        "oidc_client_id": cfg.get("client_id", ""),
        "oidc_has_secret": bool(cfg.get("client_secret")),
        "oidc_enabled": cfg.get("enabled", False),
        "oidc_disable_password": cfg.get("disable_password", False),
        "oidc_error": oidc_error,
        "oidc_redirect_uri": str(request.url_for("oidc_callback")),
    }


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    cred = _humble_credential(db)
    context = {
        "humble_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "humble_error": cred.last_error if cred else None,
    }
    context.update(_oidc_context(request, db))
    context.update(_steam_context(db))
    context.update(_gog_context(db))
    return templates.TemplateResponse(request, "settings/index.html", context)


@router.post("/humble-key", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_humble_key(request: Request, session_key: str = Form(...), db: Session = Depends(get_db)):
    session_key = session_key.strip()
    result = await HumbleConnector({"session_key": session_key}).check_credentials()
    cred = _get_or_create_humble_credential(db)

    if result.ok:
        cred.encrypted_payload = encrypt_json({"session_key": session_key})
        cred.status = STATUS_OK
        cred.last_error = None
        db.commit()

        # humble-cli itself hardcodes this path/format (plaintext, 0600) — write it
        # directly so subprocess calls to the real binary authenticate without ever
        # invoking `humble-cli auth` interactively. See config.humble_cli_key_path
        # for why this must never point at a real ~/.humble-cli-key during dev.
        key_path = settings.humble_cli_key_path
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(session_key, encoding="utf-8")
        os.chmod(key_path, 0o600)
    else:
        cred.status = STATUS_ERROR
        cred.last_error = result.message
        db.commit()

    return templates.TemplateResponse(
        request, "settings/_humble_form.html", {"humble_status": cred.status, "humble_error": cred.last_error}
    )


@router.post("/oidc", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_oidc(
    request: Request,
    issuer: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    enabled: bool = Form(False),
    disable_password: bool = Form(False),
    db: Session = Depends(get_db),
):
    issuer, client_id, client_secret = issuer.strip(), client_id.strip(), client_secret.strip()
    cred = _get_or_create_oidc_credential(db)
    existing = get_oidc_config(db) or {}
    effective_secret = client_secret or existing.get("client_secret", "")

    error = None
    if enabled:
        if not issuer or not client_id or not effective_secret:
            error = "Issuer, Client ID, and Client Secret are all required to enable SSO."
        else:
            try:
                await discover(issuer)
            except Exception as exc:
                error = f"Could not verify issuer: {exc}"

    if disable_password and (error or not enabled):
        # Never let this request turn off the only login method that's actually verified —
        # this is the exact self-lockout this feature was built to avoid.
        disable_password = False
        error = error or "SSO must be enabled and verified before password login can be turned off."

    cred.encrypted_payload = encrypt_json(
        {
            "issuer": issuer,
            "client_id": client_id,
            "client_secret": effective_secret,
            "enabled": enabled and not error,
            "disable_password": disable_password,
        }
    )
    cred.status = STATUS_ERROR if error else (STATUS_OK if enabled else STATUS_NOT_CONFIGURED)
    cred.last_error = error
    db.commit()

    return templates.TemplateResponse(request, "settings/_oidc_form.html", _oidc_context(request, db, error))


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

    context = _steam_context(db)
    context["steam_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_steam_form.html", context)


@router.post("/humble/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_humble(request: Request, db: Session = Depends(get_db)):
    cred = _humble_credential(db)
    if cred:
        db.delete(cred)
        db.commit()

    # humble-cli reads this plaintext file directly (see save_humble_key above) —
    # leaving it behind after "disconnect" would mean the CLI subprocess (and
    # anyone with filesystem access) could still authenticate as this account.
    settings.humble_cli_key_path.unlink(missing_ok=True)

    return templates.TemplateResponse(
        request, "settings/_humble_form.html", {"humble_status": STATUS_NOT_CONFIGURED, "humble_error": None}
    )


@router.post("/steam/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_steam(request: Request, db: Session = Depends(get_db)):
    cred = db.query(Credential).filter(Credential.source == SOURCE_STEAM).one_or_none()
    if cred:
        db.delete(cred)
        db.commit()
    return templates.TemplateResponse(request, "settings/_steam_form.html", _steam_context(db))


@router.post("/gog", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_gog(request: Request, pasted_code: str = Form(""), db: Session = Depends(get_db)):
    code = gog_connector.extract_code(pasted_code)

    error = None
    if not code:
        error = "Paste the URL or code you were redirected to after logging in."
    else:
        try:
            tokens = await gog_connector.exchange_code(code)
        except gog_connector.GogAuthError as exc:
            error = str(exc)
        else:
            gog_sync.save_refresh_token(db, tokens["refresh_token"])

    cred = _get_or_create_gog_credential(db)
    cred.status = STATUS_ERROR if error else STATUS_OK
    cred.last_error = error
    db.commit()

    return templates.TemplateResponse(request, "settings/_gog_form.html", _gog_context(db))


@router.post("/gog/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_gog(request: Request, db: Session = Depends(get_db)):
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one_or_none()
    if cred:
        db.delete(cred)
        db.commit()
    return templates.TemplateResponse(request, "settings/_gog_form.html", _gog_context(db))
