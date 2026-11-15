"""Refreshes the cached Navidrome library — same role sync/plex_sync.py
plays for Plex. Manual, on-demand (POST /settings/navidrome/refresh),
"nothing runs automatically".
"""


from sqlalchemy.orm import Session

from app.audible.scan import normalize
from app.connectors import navidrome_connector
from app.models.credential import SOURCE_NAVIDROME, Credential
from app.models.navidrome_album import NavidromeAlbum
from app.sync.media_cache import refresh_media_cache


class NotConnectedError(Exception):
    """Raised when no Navidrome credential has been saved yet."""


def get_navidrome_credential(db: Session) -> dict | None:
    return Credential.get_payload(db, SOURCE_NAVIDROME)


async def refresh_navidrome_library(db: Session) -> int:
    """Returns the number of albums fetched. Raises NotConnectedError or
    ValueError (bad credentials, unreachable server) — caller surfaces the
    message."""
    return await refresh_media_cache(
        db,
        source=SOURCE_NAVIDROME,
        payload=get_navidrome_credential(db),
        not_connected=NotConnectedError("Navidrome is not connected yet — add its server URL and login in Settings."),
        model=NavidromeAlbum,
        fetch=lambda p: navidrome_connector.fetch_albums(p["base_url"], p["username"], p["password"]),
        to_row=lambda album, now: NavidromeAlbum(
            album_id=album.album_id, name=album.name, artist=album.artist, fetched_at=now
        ),
    )


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
