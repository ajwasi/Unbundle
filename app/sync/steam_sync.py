"""Refreshes the cached Steam library and re-matches it against every already-
synced BundleEntitlement. Deliberately its own module, not folded into
sync/refresh.py — this isn't "sync a bundle source," it's "refresh a
comparison target and re-run a match," a different shape entirely. Manual,
on-demand (POST /steam/refresh), same "nothing runs automatically" philosophy
as the Humble refresh — and fast enough (one API call) that it doesn't need
that refresh's background-task/polling treatment.
"""


import json

from sqlalchemy.orm import Session

from app.audible.scan import normalize
from app.connectors import steam_connector
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_STEAM, STATUS_ERROR, STATUS_OK, Credential
from app.models.steam_game import SteamGame
from app.timeutil import utcnow


class NotConnectedError(Exception):
    """Raised when no Steam credential has been saved yet."""


def get_steam_credential(db: Session) -> dict | None:
    return Credential.get_payload(db, SOURCE_STEAM)


def match_entitlements_to_steam(db: Session) -> int:
    """Sets BundleEntitlement.steam_owned from the currently-cached SteamGame
    table. Returns the number of entitlement rows examined.

    Two signals, either one is enough to call a key owned:
      * the key's steam_app_id is in the library (exact, preferred);
      * the key's name equals a library game's name (case/punctuation
        insensitive). Humble's steam_app_id is frequently stale or points at a
        different listing than the one actually bought — confirmed against a
        real library: Kentucky Route Zero (25408 vs 231200), Psychonauts
        (607080 vs 3830) and others were reported "not owned" while sitting
        in the library under another appid. Never the reverse: a title match
        can only turn a "no" into a "yes".

    Steam keys with no steam_app_id (multi-game combo keys — see
    steam_connector.py's docstring) are matched by name alone: a hit sets
    steam_owned True, no hit leaves it NULL ("not yet checked", not "no"). Keys
    of other platforms are never touched — an Origin key sharing a title with
    a Steam game says nothing about the Steam library.
    """
    games = db.query(SteamGame.appid, SteamGame.name).all()
    owned_appids = {appid for appid, _ in games}
    owned_titles = {n for _, name in games if (n := normalize(name or ""))}

    examined = 0
    for row in db.query(BundleEntitlement).all():
        title_match = normalize(row.key_name or "") in owned_titles
        if row.steam_app_id is not None:
            examined += 1
            try:
                id_match = int(row.steam_app_id) in owned_appids
            except (TypeError, ValueError):
                continue
            row.steam_owned = id_match or title_match
        elif _key_type(row) == "steam":
            examined += 1
            row.steam_owned = True if title_match else None
    db.commit()
    return examined


def _key_type(row: BundleEntitlement) -> str | None:
    try:
        return (json.loads(row.raw_json) if row.raw_json else {}).get("key_type")
    except (ValueError, TypeError):
        return None


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
    now = utcnow()
    for g in games:
        db.add(SteamGame(appid=g.appid, name=g.name, playtime_forever_minutes=g.playtime_forever_minutes, img_icon_url=g.img_icon_url, fetched_at=now))
    db.commit()

    _set_credential_status(db, STATUS_OK, None)
    match_entitlements_to_steam(db)
    return len(games)


def _set_credential_status(db: Session, status: str, error: str | None) -> None:
    Credential.set_status(db, SOURCE_STEAM, status, error)
