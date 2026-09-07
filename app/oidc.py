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
from app.security import decrypt_json

OIDC_SCOPES = "openid profile email"


def get_oidc_config(db: Session) -> dict | None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one_or_none()
    if not cred or not cred.encrypted_payload:
        return None
    return decrypt_json(cred.encrypted_payload)


def is_oidc_enabled(db: Session) -> bool:
    cfg = get_oidc_config(db)
    return bool(cfg and cfg.get("enabled"))


def is_password_disabled(db: Session) -> bool:
    cfg = get_oidc_config(db)
    return bool(cfg and cfg.get("enabled") and cfg.get("disable_password"))


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
