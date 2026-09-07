"""Refreshes the cached Steam library and re-matches it against every already-
synced BundleEntitlement. Deliberately its own module, not folded into
sync/refresh.py — this isn't "sync a bundle source," it's "refresh a
comparison target and re-run a match," a different shape entirely. Manual,
on-demand (POST /steam/refresh), same "nothing runs automatically" philosophy
as the Humble refresh — and fast enough (one API call) that it doesn't need
that refresh's background-task/polling treatment.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.connectors import steam_connector
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_STEAM, STATUS_ERROR, STATUS_OK, Credential
from app.models.steam_game import SteamGame
from app.security import decrypt_json


class NotConnectedError(Exception):
    """Raised when no Steam credential has been saved yet."""


def get_steam_credential(db: Session) -> dict | None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_STEAM).one_or_none()
    if cred is None or not cred.encrypted_payload:
        return None
    return decrypt_json(cred.encrypted_payload)


def match_entitlements_to_steam(db: Session) -> int:
    """Sets BundleEntitlement.steam_owned for every row with a known
    steam_app_id, based on the currently-cached SteamGame table. Returns the
    number of entitlement rows updated. Rows with no steam_app_id (non-Steam
    keys, or multi-game combo keys — see steam_connector.py's docstring) are
    left untouched (steam_owned stays NULL: "not yet checked", not "no").
    """
    owned_appids = {g.appid for g in db.query(SteamGame.appid).all()}
    rows = db.query(BundleEntitlement).filter(BundleEntitlement.steam_app_id.isnot(None)).all()
    for row in rows:
        try:
            appid = int(row.steam_app_id)
        except (TypeError, ValueError):
            continue
        row.steam_owned = appid in owned_appids
    db.commit()
    return len(rows)


async def refresh_steam_library(db: Session) -> int:
    """Returns the number of games fetched. Raises NotConnectedError or
    ValueError (private profile, bad key) — caller surfaces the message."""
    payload = get_steam_credential(db)
    if not payload:
        raise NotConnectedError("Steam is not connected yet — add your API key and SteamID in Settings.")

    try:
        games = await steam_connector.fetch_owned_games(payload["api_key"], payload["steamid64"])
    except Exception as exc:
        _set_credential_status(db, STATUS_ERROR, str(exc))
        raise

    db.query(SteamGame).delete()
    now = datetime.utcnow()
    for g in games:
        db.add(SteamGame(appid=g.appid, name=g.name, playtime_forever_minutes=g.playtime_forever_minutes, img_icon_url=g.img_icon_url, fetched_at=now))
    db.commit()

    _set_credential_status(db, STATUS_OK, None)
    match_entitlements_to_steam(db)
    return len(games)


def _set_credential_status(db: Session, status: str, error: str | None) -> None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_STEAM).one_or_none()
    if cred:
        cred.status = status
        cred.last_error = error
        db.commit()
