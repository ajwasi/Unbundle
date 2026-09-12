"""Shared entitlement-display logic: expiration parsing and cross-platform,
name-based "you might already own this" hints — used by the bundle detail
page and the Steam/GOG unredeemed-keys pages alike.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.connectors.gog_connector import CONTENT_TYPE_GAME
from app.models.gog_game import GogGame
from app.models.steam_game import SteamGame


def parse_expiration(raw: dict) -> datetime | None:
    """The entitlement's expiration date/time (UTC — Humble's own value carries
    no timezone marker, treated as UTC, unverified), or None if this key never
    expires (no expiration_date/expiry_date field at all) or the value is
    unparseable.
    """
    raw_date = raw.get("expiration_date") or raw.get("expiry_date")
    if not raw_date:
        return None
    try:
        return datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def owned_title_sets(db: Session) -> tuple[set[str], set[str]]:
    """(steam_titles, gog_titles) — every owned game's display name/title,
    casefolded, from each synced library. Lets a caller flag "you might
    already own this under a different listing" even when the entitlement's
    own recorded platform-specific ID doesn't match anything — a different
    edition/re-release under a different Steam appid, for instance, or a
    game whose gog_id was never populated in the first place (see
    sync/gog_sync.py's own docstring on how rare a real gog_id actually is).
    """
    steam_titles = {name.strip().casefold() for (name,) in db.query(SteamGame.name).all()}
    # GogGame now also holds movies (see its own docstring) — only games are a
    # plausible "you might already own this" match for a Steam/Humble game key.
    gog_titles = {
        title.strip().casefold()
        for (title,) in db.query(GogGame.title).filter(GogGame.content_type == CONTENT_TYPE_GAME).all()
    }
    return steam_titles, gog_titles
