"""Refreshes the cached GOG library and re-matches it against BundleEntitlement
rows — same shape as sync/steam_sync.py. Only the stored refresh_token is
persisted (see connectors/gog_connector.py); a fresh access_token is minted on
every refresh rather than cached, since the whole refresh is one quick
operation and access tokens are only good for about an hour anyway.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.connectors import gog_connector
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_GOG, STATUS_ERROR, STATUS_OK, Credential
from app.models.gog_game import GogGame
from app.security import decrypt_json, encrypt_json


class NotConnectedError(Exception):
    """Raised when no GOG credential has been saved yet."""


def get_gog_credential(db: Session) -> dict | None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one_or_none()
    if cred is None or not cred.encrypted_payload:
        return None
    return decrypt_json(cred.encrypted_payload)


def match_entitlements_to_gog(db: Session) -> int:
    """Same logic as steam_sync.match_entitlements_to_steam, against gog_id
    instead — see BundleEntitlement's docstring for why this affects very
    few real rows today (Humble rarely populates gog_id at all).
    """
    owned_ids = {g.product_id for g in db.query(GogGame.product_id).all()}
    rows = db.query(BundleEntitlement).filter(BundleEntitlement.gog_id.isnot(None)).all()
    for row in rows:
        try:
            product_id = int(row.gog_id)
        except (TypeError, ValueError):
            continue
        row.gog_owned = product_id in owned_ids
    db.commit()
    return len(rows)


def save_refresh_token(db: Session, refresh_token: str) -> None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one_or_none()
    if cred is None:
        cred = Credential(source=SOURCE_GOG)
        db.add(cred)
    cred.encrypted_payload = encrypt_json({"refresh_token": refresh_token})
    db.commit()


async def refresh_gog_library(db: Session) -> int:
    """Returns the number of games fetched. Raises NotConnectedError or
    gog_connector.GogAuthError — caller surfaces the message."""
    payload = get_gog_credential(db)
    if not payload:
        raise NotConnectedError("GOG is not connected yet — connect it in Settings.")

    try:
        tokens = await gog_connector.refresh_access_token(payload["refresh_token"])
        # GOG may rotate the refresh token on each use — persist whatever came
        # back so the next refresh doesn't fail with a stale one.
        if tokens.get("refresh_token"):
            save_refresh_token(db, tokens["refresh_token"])
        games = await gog_connector.fetch_owned_games(tokens["access_token"])
    except Exception as exc:
        _set_credential_status(db, STATUS_ERROR, str(exc))
        raise

    db.query(GogGame).delete()
    now = datetime.utcnow()
    for g in games:
        db.add(GogGame(product_id=g.product_id, title=g.title, image_url=g.image_url, fetched_at=now))
    db.commit()

    _set_credential_status(db, STATUS_OK, None)
    match_entitlements_to_gog(db)
    return len(games)


def _set_credential_status(db: Session, status: str, error: str | None) -> None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one_or_none()
    if cred:
        cred.status = status
        cred.last_error = error
        db.commit()
