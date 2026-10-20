"""Plex Media Server — a plain base_url + X-Plex-Token connection (the token
minted from the account's own authorized-devices page, or lifted from any
request Plex Web already makes — see Settings' own how-to text), same shape
as audiobookshelf_connector.py. Read-only.

Plex has no dedicated "audiobook" library type — the overwhelmingly common
community setup houses audiobooks in a plain Music ("artist"-type) library,
author-as-artist, book-as-album, chapters-as-tracks. Confirmed via Plex's own
long-stable, widely-relied-on (python-plexapi) numeric `type` query param on
/library/sections/{key}/all: 8=artist, 9=album, 10=track — not independently
re-verified live against this app's own account. An album (type 9) is the
right level for "is this book present at all": one row per book regardless
of how many chapter tracks it has, with the artist (author) as its own
parent. There is no ASIN or other stable external id on any of this, so
matching against AudibleBook happens by normalized title only — see
sync/plex_sync.py.

Because "artist"-type is also what a real music library looks like at the
section-list level, this app cannot safely auto-pick "the" audiobook
library the way audiobookshelf_connector.py picks the first mediaType
"book" library — the section key is a required field here, and
check_credentials() lists every section (key + title + type) so the user
can find the right one.
"""

import httpx

from app.connectors.types import CredentialStatus

_TIMEOUT_SECONDS = 15
_TYPE_ALBUM = 9


class PlexLibraryData:
    def __init__(self, key: str, title: str, type: str):
        self.key = key
        self.title = title
        self.type = type


class PlexAudiobookItemData:
    def __init__(self, rating_key: str, title: str, author: str):
        self.rating_key = rating_key
        self.title = title
        self.author = author


def _headers(token: str) -> dict:
    return {"X-Plex-Token": token, "Accept": "application/json"}


def _normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


async def fetch_libraries(base_url: str, token: str) -> list[PlexLibraryData]:
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{_normalize_base_url(base_url)}/library/sections", headers=_headers(token))
    if resp.status_code == 401:
        raise ValueError("Plex rejected this token.")
    if resp.status_code != 200:
        raise ValueError(f"Plex check failed: HTTP {resp.status_code}")
    directories = ((resp.json() or {}).get("MediaContainer") or {}).get("Directory") or []
    return [
        PlexLibraryData(key=str(d.get("key") or ""), title=d.get("title", ""), type=d.get("type", ""))
        for d in directories
        if d.get("key")
    ]


async def check_credentials(base_url: str, token: str) -> CredentialStatus:
    if not base_url.strip():
        return CredentialStatus(ok=False, message="Server URL is required.")
    try:
        libraries = await fetch_libraries(base_url, token)
    except httpx.HTTPError as exc:
        return CredentialStatus(ok=False, message=f"Could not reach Plex: {exc}")
    except ValueError as exc:
        return CredentialStatus(ok=False, message=str(exc))

    if not libraries:
        return CredentialStatus(ok=True, message="Connected, but this server has no libraries yet.")
    listing = ", ".join(f"{lib.title} (key {lib.key}, type {lib.type})" for lib in libraries)
    return CredentialStatus(ok=True, message=f"Connected — libraries: {listing}")


async def fetch_audiobook_items(base_url: str, token: str, library_id: str) -> list[PlexAudiobookItemData]:
    """Every album (book) in the given library section. library_id is
    required — see this module's own docstring for why Plex can't be
    auto-detected the way AudiobookShelf is.
    """
    library_id = library_id.strip()
    if not library_id:
        raise ValueError(
            "A Plex library section ID is required — check Plex above for the list of your server's "
            "libraries and their keys, and enter the one that holds your audiobooks."
        )

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{_normalize_base_url(base_url)}/library/sections/{library_id}/all",
            headers=_headers(token),
            params={"type": _TYPE_ALBUM},
        )
    if resp.status_code != 200:
        raise ValueError(f"Plex rejected the library-items request: HTTP {resp.status_code}")

    albums = ((resp.json() or {}).get("MediaContainer") or {}).get("Metadata") or []
    items = [
        PlexAudiobookItemData(
            rating_key=str(a.get("ratingKey") or ""),
            title=str(a.get("title") or ""),
            author=str(a.get("parentTitle") or ""),
        )
        for a in albums
    ]
    return [item for item in items if item.rating_key]
