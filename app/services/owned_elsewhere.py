"""Which wishlist items you already own through a *different* service.

A Steam wishlist game you own on GOG, a game whose key is sitting unredeemed
in a Humble bundle, an Audible wishlist book that's already in Chirp or on your
AudiobookShelf server: all "you've effectively bought this" without the store
whose wishlist it's on knowing. Matching is by normalized title
(app.audible.scan.normalize: case and punctuation insensitive, exact
otherwise) because the stores share no common id. A different edition under a
different name ("... Game of the Year Edition") therefore won't match; a false
negative just means no highlight, whereas fuzzy matching would risk flagging
the wrong game.

The item's own store is never counted here — "Owned" in the same store is a
separate, existing badge on the wishlist page.
"""

from sqlalchemy.orm import Session

from app.audible.scan import normalize
from app.connectors.gog_connector import CONTENT_TYPE_GAME
from app.models.audible_book import AudibleBook
from app.models.audiobookshelf_item import AudiobookShelfItem
from app.models.bundle_entitlement import BundleEntitlement
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.gog_game import GogGame
from app.models.plex_audiobook_item import PlexAudiobookItem
from app.models.steam_game import SteamGame
from app.services.catalog import build_catalog

HUMBLE = "Humble Bundle"


def _titles(rows) -> set[str]:
    return {n for (title,) in rows if (n := normalize(title or ""))}


def _library_titles(db: Session, service: str) -> set[str]:
    """Normalized titles of everything owned on `service`."""
    if service == "Steam":
        return _titles(db.query(SteamGame.name))
    if service == "GOG":
        # GogGame also holds movies; only games are a plausible match.
        return _titles(db.query(GogGame.title).filter(GogGame.content_type == CONTENT_TYPE_GAME))
    if service == "Audible":
        return _titles(db.query(AudibleBook.title))
    if service == "Chirp":
        return _titles(db.query(ChirpAudiobook.title))
    if service == "AudiobookShelf":
        return _titles(db.query(AudiobookShelfItem.title))
    if service == "Plex":
        return _titles(db.query(PlexAudiobookItem.title))
    if service == HUMBLE:
        # Keys (Steam/GOG/other) inside bundles, redeemed or not, plus the
        # DRM-free items every bundle delivered.
        keys = _titles(db.query(BundleEntitlement.key_name))
        items = {n for entry in build_catalog(db).values() if (n := normalize(entry["item_name"]))}
        return keys | items
    raise ValueError(service)


# Every library a wishlist item could already be owned in, per wishlist store,
# minus the store itself.
_OTHER_SERVICES = {
    "steam": ("GOG", HUMBLE),
    "gog": ("Steam", HUMBLE),
    "audible": ("Chirp", "AudiobookShelf", "Plex", HUMBLE),
    "chirp": ("Audible", "AudiobookShelf", "Plex", HUMBLE),
}


def owned_elsewhere(db: Session, source: str, items: list, title_of, key_of) -> dict:
    """{key_of(item): [service, ...]} for items owned on at least one other
    service; items with no match are absent."""
    if not items:
        return {}
    libraries = {service: _library_titles(db, service) for service in _OTHER_SERVICES[source]}
    found: dict = {}
    for item in items:
        needle = normalize(title_of(item) or "")
        if not needle:
            continue
        services = [service for service, titles in libraries.items() if needle in titles]
        if services:
            found[key_of(item)] = services
    return found
