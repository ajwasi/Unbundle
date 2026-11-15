"""Refreshes the cached Plex audiobook library — same role sync/
audiobookshelf_sync.py plays for AudiobookShelf. Manual, on-demand
(POST /settings/plex/refresh), "nothing runs automatically".
"""


from sqlalchemy.orm import Session

from app.audible.scan import normalize
from app.connectors import plex_connector
from app.models.credential import SOURCE_PLEX, Credential
from app.models.plex_audiobook_item import PlexAudiobookItem
from app.sync.media_cache import refresh_media_cache


class NotConnectedError(Exception):
    """Raised when no Plex credential has been saved yet."""


def get_plex_credential(db: Session) -> dict | None:
    return Credential.get_payload(db, SOURCE_PLEX)


async def refresh_plex_library(db: Session) -> int:
    """Returns the number of items fetched. Raises NotConnectedError or
    ValueError (bad token, unreachable server, no library_id configured) —
    caller surfaces the message."""
    return await refresh_media_cache(
        db,
        source=SOURCE_PLEX,
        payload=get_plex_credential(db),
        not_connected=NotConnectedError("Plex is not connected yet — add its server URL and token in Settings."),
        model=PlexAudiobookItem,
        fetch=lambda p: plex_connector.fetch_audiobook_items(p["base_url"], p["token"], p.get("library_id", "")),
        to_row=lambda item, now: PlexAudiobookItem(
            rating_key=item.rating_key, title=item.title, author=item.author, fetched_at=now
        ),
    )


def owned_normalized_titles(db: Session) -> set[str]:
    """Normalized (app.audible.scan.normalize) titles of every book
    currently cached from the last Plex refresh — what routers/audible.py
    checks each owned book's own normalized title against to render the
    "In Plex" badge. Plex has no ASIN, so this is the only reliable match
    key it and Audible's catalog both carry.
    """
    return {normalize(title) for (title,) in db.query(PlexAudiobookItem.title).all() if normalize(title)}
