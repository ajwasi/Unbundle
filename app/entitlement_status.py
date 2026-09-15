"""Shared entitlement-display logic: expiration parsing and cross-platform,
name-based "you might already own this" hints — used by the bundle detail
page and the Steam/GOG unredeemed-keys pages alike.
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.connectors.gog_connector import CONTENT_TYPE_GAME
from app.connectors.humble_connector import order_page_url
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
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


def unredeemed_rows(db: Session, platform: str) -> list[dict]:
    """Rows from BundleEntitlement not yet confirmed owned on `platform`
    ("steam" or "gog"), each carrying the cross-platform "you might already
    own this" hints steam.py's/gog.py's own unredeemed-keys pages show.
    Extracted from what were two ~90%-identical copies in
    routers/steam.py/routers/gog.py — the only real differences were which
    owned-column to filter on, and which owned-title-set plays "self" vs
    "other platform" in the result. Dict keys below are built from
    `platform` on purpose (owned_as_different_steam_listing /
    owned_as_different_gog_listing, owned_on_gog / owned_on_steam) — these
    exact names are what steam/_content.html and gog/_content.html already
    read from their own template context, unchanged by this extraction.
    """
    if platform == "steam":
        owned_filter = [BundleEntitlement.steam_app_id.isnot(None), BundleEntitlement.steam_owned.is_(False)]
        other_platform = "gog"
    elif platform == "gog":
        # No gog_id.isnot(None) requirement — sync/gog_sync.py falls back to
        # name-matching when gog_id is absent (virtually always), so
        # gog_owned alone is the correct "was this checked" signal.
        owned_filter = [BundleEntitlement.gog_owned.is_(False)]
        other_platform = "steam"
    else:
        raise ValueError(f"Unknown platform: {platform!r}")

    rows = (
        db.query(BundleEntitlement, Bundle)
        .join(Bundle, Bundle.gamekey == BundleEntitlement.gamekey)
        .filter(*owned_filter)
        .order_by(Bundle.name)
        .all()
    )
    owned_steam_titles, owned_gog_titles = owned_title_sets(db)
    self_titles = owned_steam_titles if platform == "steam" else owned_gog_titles
    other_titles = owned_gog_titles if platform == "steam" else owned_steam_titles

    result = []
    for ent, bundle in rows:
        try:
            raw = json.loads(ent.raw_json) if ent.raw_json else {}
        except (ValueError, TypeError):
            raw = {}
        expires_at = parse_expiration(raw)
        title = ent.key_name.strip().casefold()
        result.append(
            {
                "key_name": ent.key_name,
                "gamekey": ent.gamekey,
                "bundle_name": bundle.name,
                "redeemed_on_humble": ent.redeemed_on_humble,
                "redeem_url": order_page_url(ent.gamekey),
                f"owned_as_different_{platform}_listing": title in self_titles,
                f"owned_on_{other_platform}": title in other_titles,
                "expires_at": expires_at,
                "days_until_expired": (expires_at - datetime.now(timezone.utc)).days if expires_at else None,
                "is_expired": expires_at is not None and expires_at < datetime.now(timezone.utc),
            }
        )
    return result
