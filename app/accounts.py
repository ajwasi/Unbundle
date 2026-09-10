"""The display identity shown in the top-right user menu. OIDC sessions carry
their own identity (see oidc.py:identity_from_userinfo); the plain-password
login path has no identity of its own (it verifies a shared secret, not a
person), so AccountSettings.email is an admin-set label for that case.
"""

from sqlalchemy.orm import Session

from app.models.account_settings import AccountSettings

_FALLBACK_LABEL = "Account"


def get_or_create_account_settings(db: Session) -> AccountSettings:
    cfg = db.get(AccountSettings, 1)
    if cfg is None:
        cfg = AccountSettings(id=1)
        db.add(cfg)
        db.commit()
    return cfg


def resolve_identity_label(session_identity: dict | None, db: Session) -> str:
    """A live SSO session always wins over the stored AccountSettings.email —
    it reflects who's actually logged in right now."""
    if session_identity:
        label = session_identity.get("email") or session_identity.get("name") or session_identity.get("sub")
        if label:
            return label
    cfg = get_or_create_account_settings(db)
    return cfg.email or _FALLBACK_LABEL
