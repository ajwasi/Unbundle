"""AudiobookShelf — a self-hosted audiobook/podcast server with a plain REST
API, Bearer-token authenticated (a key minted in its own Settings > Users >
API Keys, not an OAuth dance). Read-only: this app only ever lists a
library's items to flag which Audible/Humble titles are already present
there — see sync/audiobookshelf_sync.py for the match itself.

Endpoint shapes below are AudiobookShelf's own documented API
(api.audiobookshelf.org), not reverse-engineered — that page itself notes
its docs are no longer actively maintained, so treat field names as
"documented, not independently confirmed live" the same way steam_connector.py
already flags Steam's.
"""

import httpx

from app.connectors.types import CredentialStatus

_TIMEOUT_SECONDS = 15


class AudiobookShelfLibraryData:
    def __init__(self, id: str, name: str, media_type: str):
        self.id = id
        self.name = name
        self.media_type = media_type


class AudiobookShelfItemData:
    def __init__(self, library_item_id: str, asin: str, title: str, author: str):
        self.library_item_id = library_item_id
        self.asin = asin
        self.title = title
        self.author = author


def _headers(api_token: str) -> dict:
    return {"Authorization": f"Bearer {api_token}"}


def _normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


async def fetch_libraries(base_url: str, api_token: str) -> list[AudiobookShelfLibraryData]:
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{_normalize_base_url(base_url)}/api/libraries", headers=_headers(api_token))
    if resp.status_code == 401:
        raise ValueError("AudiobookShelf rejected this API token.")
    if resp.status_code != 200:
        raise ValueError(f"AudiobookShelf check failed: HTTP {resp.status_code}")
    libraries = (resp.json() or {}).get("libraries") or []
    return [
        AudiobookShelfLibraryData(id=lib.get("id", ""), name=lib.get("name", ""), media_type=lib.get("mediaType", ""))
        for lib in libraries
        if lib.get("id")
    ]


async def check_credentials(base_url: str, api_token: str) -> CredentialStatus:
    if not base_url.strip():
        return CredentialStatus(ok=False, message="Server URL is required.")
    try:
        libraries = await fetch_libraries(base_url, api_token)
    except httpx.HTTPError as exc:
        return CredentialStatus(ok=False, message=f"Could not reach AudiobookShelf: {exc}")
    except ValueError as exc:
        return CredentialStatus(ok=False, message=str(exc))

    book_libraries = [lib for lib in libraries if lib.media_type == "book"]
    if not book_libraries:
        return CredentialStatus(
            ok=False,
            message="Connected, but no audiobook library (mediaType \"book\") was found on this server.",
        )
    names = ", ".join(lib.name for lib in book_libraries)
    return CredentialStatus(ok=True, message=f"Connected — audiobook librar{'y' if len(book_libraries) == 1 else 'ies'}: {names}")


async def fetch_library_items(base_url: str, api_token: str, library_id: str = "") -> list[AudiobookShelfItemData]:
    """Items from the given library, or — when library_id is blank — the
    first library whose mediaType is "book" (AudiobookShelf's own marker for
    an audiobook library, as opposed to "podcast"). Raises ValueError if
    library_id is blank and no book library can be found.
    """
    resolved_id = library_id.strip()
    if not resolved_id:
        libraries = await fetch_libraries(base_url, api_token)
        book_library = next((lib for lib in libraries if lib.media_type == "book"), None)
        if book_library is None:
            raise ValueError("No audiobook library (mediaType \"book\") found on this AudiobookShelf server.")
        resolved_id = book_library.id

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{_normalize_base_url(base_url)}/api/libraries/{resolved_id}/items",
            headers=_headers(api_token),
            params={"minified": 1},
        )
    if resp.status_code != 200:
        raise ValueError(f"AudiobookShelf rejected the library-items request: HTTP {resp.status_code}")

    items = []
    for row in (resp.json() or {}).get("results") or []:
        metadata = ((row.get("media") or {}).get("metadata")) or {}
        items.append(
            AudiobookShelfItemData(
                library_item_id=str(row.get("id") or ""),
                asin=str(metadata.get("asin") or "").strip(),
                title=str(metadata.get("title") or ""),
                author=str(metadata.get("authorName") or ""),
            )
        )
    return [item for item in items if item.library_item_id]
