"""Refreshes the cached AudiobookShelf library — a comparison target, same
role sync/steam_sync.py plays for Steam, except the match it feeds is
Audible's own owned-books table rather than BundleEntitlement (see
routers/audible.py's _context(), which reads AudiobookShelfItem.asin
membership directly rather than going through a separate match function —
there's no per-row flag to set on AudibleBook itself, just a set checked at
render time, so there is nothing here to mirror steam_sync's own
match_entitlements_to_steam() for). Manual, on-demand
(POST /settings/audiobookshelf/refresh), same "nothing runs automatically"
philosophy, and fast enough (one or two API calls) to run inline in the
request.
"""


from sqlalchemy.orm import Session

from app.connectors import audiobookshelf_connector
from app.models.audiobookshelf_item import AudiobookShelfItem
from app.models.credential import SOURCE_AUDIOBOOKSHELF, Credential
from app.sync.media_cache import refresh_media_cache


class NotConnectedError(Exception):
    """Raised when no AudiobookShelf credential has been saved yet."""


def get_audiobookshelf_credential(db: Session) -> dict | None:
    return Credential.get_payload(db, SOURCE_AUDIOBOOKSHELF)


async def refresh_audiobookshelf_library(db: Session) -> int:
    """Returns the number of items fetched. Raises NotConnectedError or
    ValueError (bad token, unreachable server, no book library) — caller
    surfaces the message."""
    return await refresh_media_cache(
        db,
        source=SOURCE_AUDIOBOOKSHELF,
        payload=get_audiobookshelf_credential(db),
        not_connected=NotConnectedError(
            "AudiobookShelf is not connected yet — add its server URL and API token in Settings."
        ),
        model=AudiobookShelfItem,
        fetch=lambda p: audiobookshelf_connector.fetch_library_items(
            p["base_url"], p["api_token"], p.get("library_id", "")
        ),
        to_row=lambda item, now: AudiobookShelfItem(
            library_item_id=item.library_item_id,
            asin=item.asin or None,
            title=item.title,
            author=item.author,
            fetched_at=now,
        ),
    )


def owned_asins(db: Session) -> set[str]:
    """Every ASIN currently cached from the last AudiobookShelf refresh —
    what routers/audible.py checks each owned book's asin against to render
    the "In AudiobookShelf" badge."""
    return {asin for (asin,) in db.query(AudiobookShelfItem.asin).filter(AudiobookShelfItem.asin.isnot(None)).all()}
