"""Navidrome — a self-hosted music server implementing the (long-stable,
widely-relied-on) Subsonic API, not a bespoke API of its own. Confirmed
against subsonic.org's own spec (not independently re-verified live against
this app's own account): every request takes u/t/s/v/c as common params
(username, a salted token = md5(password + salt), the salt itself, protocol
version, client name), plus f=json for a JSON response instead of the
default XML. Read-only.

Uses getAlbumList2 (type=alphabeticalByName), not search3 with an empty
query, to enumerate the whole library — search3's behaviour on an empty
query isn't part of the documented contract, while getAlbumList2 is
explicitly the paging-through-everything endpoint (capped at 500 per page
per the spec, hence the loop in fetch_albums).
"""

import hashlib
import secrets

import httpx

from app.connectors.types import CredentialStatus

_TIMEOUT_SECONDS = 15
_API_VERSION = "1.16.1"
_CLIENT_NAME = "unbundle"
_PAGE_SIZE = 500


class NavidromeAlbumData:
    def __init__(self, album_id: str, name: str, artist: str):
        self.album_id = album_id
        self.name = name
        self.artist = artist


def _normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def _auth_params(username: str, password: str) -> dict:
    # MD5 here is the Subsonic API's own mandated scheme, not a choice made by
    # this app — the spec defines t as exactly md5(password + salt), and
    # Navidrome's server validates against that formula; a stronger hash on
    # this end would simply fail every request. A fresh, cryptographically
    # random salt (secrets.token_hex, not reused across calls) on every
    # single request is what actually carries this scheme's real security
    # property — not sending the plaintext password on the wire — rather
    # than MD5's own (otherwise weak) collision resistance; HTTPS is still
    # what protects the password and token values themselves in transit.
    salt = secrets.token_hex(6)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()  # lgtm[py/weak-sensitive-data-hashing]
    return {"u": username, "t": token, "s": salt, "v": _API_VERSION, "c": _CLIENT_NAME, "f": "json"}


async def _get(base_url: str, username: str, password: str, path: str, extra_params: dict | None = None) -> dict:
    params = _auth_params(username, password)
    if extra_params:
        params.update(extra_params)
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{_normalize_base_url(base_url)}/rest/{path}", params=params)
    if resp.status_code != 200:
        raise ValueError(f"Navidrome request failed: HTTP {resp.status_code}")

    body = (resp.json() or {}).get("subsonic-response") or {}
    if body.get("status") != "ok":
        error = body.get("error") or {}
        raise ValueError(error.get("message") or "Navidrome rejected this request.")
    return body


async def check_credentials(base_url: str, username: str, password: str) -> CredentialStatus:
    if not base_url.strip():
        return CredentialStatus(ok=False, message="Server URL is required.")
    try:
        await _get(base_url, username, password, "ping")
    except httpx.HTTPError as exc:
        return CredentialStatus(ok=False, message=f"Could not reach Navidrome: {exc}")
    except ValueError as exc:
        return CredentialStatus(ok=False, message=str(exc))
    return CredentialStatus(ok=True, message=f"Connected as {username}")


async def fetch_albums(base_url: str, username: str, password: str) -> list[NavidromeAlbumData]:
    """Every album in the library, paged via getAlbumList2 (500 per page,
    the Subsonic spec's own cap) until a short page signals the end."""
    albums: list[NavidromeAlbumData] = []
    offset = 0
    while True:
        body = await _get(
            base_url, username, password, "getAlbumList2",
            {"type": "alphabeticalByName", "size": _PAGE_SIZE, "offset": offset},
        )
        page = ((body.get("albumList2") or {}).get("album")) or []
        for a in page:
            album_id = str(a.get("id") or "")
            if album_id:
                albums.append(NavidromeAlbumData(album_id=album_id, name=str(a.get("name") or ""), artist=str(a.get("artist") or "")))
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return albums
