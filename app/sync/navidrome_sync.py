"""Refreshes the cached Navidrome library — same role sync/plex_sync.py
plays for Plex. Manual, on-demand (POST /settings/navidrome/refresh),
"nothing runs automatically".
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.audible.scan import normalize
from app.connectors import navidrome_connector
from app.models.credential import SOURCE_NAVIDROME, STATUS_ERROR, STATUS_OK, Credential
from app.models.navidrome_album import NavidromeAlbum


class NotConnectedError(Exception):
    """Raised when no Navidrome credential has been saved yet."""


def get_navidrome_credential(db: Session) -> dict | None:
    return Credential.get_payload(db, SOURCE_NAVIDROME)


async def refresh_navidrome_library(db: Session) -> int:
    """Returns the number of albums fetched. Raises NotConnectedError or
    ValueError (bad credentials, unreachable server) — caller surfaces the
    message."""
    payload = get_navidrome_credential(db)
    if not payload:
        raise NotConnectedError("Navidrome is not connected yet — add its server URL and login in Settings.")

    try:
        albums = await navidrome_connector.fetch_albums(payload["base_url"], payload["username"], payload["password"])
    except Exception as exc:
        Credential.set_status(db, SOURCE_NAVIDROME, STATUS_ERROR, str(exc))
        raise

    db.query(NavidromeAlbum).delete()
    now = datetime.utcnow()
    for album in albums:
        db.add(NavidromeAlbum(album_id=album.album_id, name=album.name, artist=album.artist, fetched_at=now))
    db.commit()

    Credential.set_status(db, SOURCE_NAVIDROME, STATUS_OK, None)
    return len(albums)


def owned_normalized_albums(db: Session) -> set[tuple[str, str]]:
    """(normalized album name, normalized artist) pairs for every album
    currently cached from the last Navidrome refresh — what
    routers/amazon_music.py checks each owned album against to render the
    "In Navidrome" badge. Pairing with artist (unlike Plex's title-only
    match) cuts down on same-named-album collisions across unrelated
    artists, which Amazon's own purchased-music catalogue is more likely to
    contain than Audible's.
    """
    return {
        (normalize(name), normalize(artist))
        for (name, artist) in db.query(NavidromeAlbum.name, NavidromeAlbum.artist).all()
        if normalize(name)
    }
