from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_OIDC, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.oidc import discover, get_oidc_config
from app.security import encrypt_json
from app.templates_env import templates

router = APIRouter()


def _get_or_create_oidc_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_OIDC)


def oidc_context(request: Request, db: Session, oidc_error: str | None = None, saved: bool = False) -> dict:
    cfg = get_oidc_config(db) or {}
    return {
        "oidc_issuer": cfg.get("issuer", ""),
        "oidc_client_id": cfg.get("client_id", ""),
        "oidc_has_secret": bool(cfg.get("client_secret")),
        "oidc_enabled": cfg.get("enabled", False),
        "oidc_disable_password": cfg.get("disable_password", False),
        "oidc_error": oidc_error,
        "oidc_redirect_uri": str(request.url_for("oidc_callback")),
        "oidc_saved": saved,
    }


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

    return templates.TemplateResponse(request, "settings/_oidc_form.html", oidc_context(request, db, error, saved=not error))
