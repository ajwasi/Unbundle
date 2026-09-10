"""OIDC (SSO) support — config is stored in the DB (Credential, source="oidc",
same encrypted-payload mechanism already used for the Humble session key) rather
than env vars, since it needs to be editable from the Settings page at runtime.

The OAuth client is built fresh per request from that stored config rather than
registered once at startup, since authlib's OAuth.register() only does lazy
discovery (no network call until authorize_redirect/authorize_access_token
actually run) — cheap enough to not need caching for a single-user tool.
"""

import httpx
from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session

from app.models.credential import SOURCE_OIDC, Credential
from app.security import decrypt_json, has_db_password

OIDC_SCOPES = "openid profile email"


def get_oidc_config(db: Session) -> dict | None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one_or_none()
    if not cred or not cred.encrypted_payload:
        return None
    return decrypt_json(cred.encrypted_payload)


def is_oidc_enabled(db: Session, cfg: dict | None = None) -> bool:
    # cfg lets a caller that already has it (is_auth_configured, below) skip a
    # redundant get_oidc_config() — pass explicitly only when you already have it.
    cfg = get_oidc_config(db) if cfg is None else cfg
    return bool(cfg and cfg.get("enabled"))


def is_password_disabled(db: Session, cfg: dict | None = None) -> bool:
    cfg = get_oidc_config(db) if cfg is None else cfg
    return bool(cfg and cfg.get("enabled") and cfg.get("disable_password"))


def is_password_login_active(db: Session, cfg: dict | None = None) -> bool:
    """True if a password (APP_PASSWORD or one set via `python -m app.cli
    set-password`) is currently a usable login method — not turned off by
    SSO-only mode. Extracted out of is_auth_configured() below since the
    Account settings card needs this exact same check to decide whether to
    show password-change fields at all.
    """
    from app.config import settings  # local import: settings has no reason to import oidc.py

    cfg = get_oidc_config(db) if cfg is None else cfg
    return (bool(settings.app_password) or has_db_password(db)) and not is_password_disabled(db, cfg)


def is_auth_configured(db: Session) -> bool:
    """True if some login method is actually usable right now — either a
    password or an enabled OIDC provider. False means the app is wide open to
    anyone who can reach it. Shared by AuthMiddleware (the actual gate),
    main.py's startup warning, and the site-wide banner in base.html — one
    definition, so they can't drift.

    Fetches the OIDC config once and passes it to both checks below rather
    than letting each call get_oidc_config() independently — this runs on
    every single request via AuthMiddleware, so the extra DB round trip and
    Fernet decrypt would otherwise be paid twice per request for no reason.
    """
    cfg = get_oidc_config(db)
    return is_password_login_active(db, cfg) or is_oidc_enabled(db, cfg)


def identity_from_userinfo(userinfo: dict) -> dict:
    """sub is always present for a real OIDC response; email/name depend on
    the provider actually returning them for the "profile email" scopes
    requested above — both are common but neither is guaranteed."""
    identity = {"sub": userinfo.get("sub")}
    if userinfo.get("email"):
        identity["email"] = userinfo["email"]
    if userinfo.get("name"):
        identity["name"] = userinfo["name"]
    return identity


def build_oauth_client(cfg: dict):
    oauth = OAuth()
    issuer = cfg["issuer"].rstrip("/")
    oauth.register(
        name="oidc",
        server_metadata_url=f"{issuer}/.well-known/openid-configuration",
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        client_kwargs={"scope": OIDC_SCOPES},
    )
    return oauth.oidc


async def discover(issuer: str) -> dict:
    """Fetches the discovery document directly (no authlib) so Settings can validate
    an issuer URL synchronously on save, before anyone attempts a real login with it.
    Raises on any failure — callers turn that into a user-facing error message.
    """
    issuer = issuer.rstrip("/")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{issuer}/.well-known/openid-configuration")
        resp.raise_for_status()
    data = resp.json()
    for key in ("authorization_endpoint", "token_endpoint"):
        if key not in data:
            raise ValueError(f"Discovery document is missing '{key}' — is this really an OIDC issuer URL?")
    return data
