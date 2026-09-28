"""Local, persistent cache for cover art whose remote URL may not always stay
reachable — Amazon Music's cover_url, confirmed, is a presigned S3 URL that
expires; once this app has fetched an image once, it should never need that
exact remote URL to still be valid again.

Cached under the URL's own host+path with the query string stripped out, not
under some caller-supplied entity id (an album_asin, say) — that's exactly
what changes between two presigned URLs for the *same* underlying image (a
fresh signature and expiry, not a new picture), so two different presigned
URLs for one album's cover hash to the same cache entry automatically, and a
genuinely new image (a different path) just as naturally gets a fresh one.
No separate invalidation logic needed either way.

Fetched lazily — the first time a viewer actually asks to see an image — not
during a sync, which already means potentially thousands of network calls;
adding one more per cover on top of that would be another order of magnitude
of requests for art most of the time nobody scrolls to.

Namespaced (a caller-chosen short string, e.g. "amazon-music-album") purely
to keep different callers' cached files apart on disk — this module knows
nothing about albums, games, or any other domain concept, only URLs.
"""

import hashlib
from pathlib import Path

import httpx

from app.config import settings

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _cache_dir(namespace: str) -> Path:
    return settings.data_dir / "asset_cache" / namespace


def _cache_key(remote_url: str) -> str:
    try:
        url = httpx.URL(remote_url)
        stable = f"{url.host}{url.path}"
    except Exception:
        stable = remote_url
    return hashlib.sha256(stable.encode()).hexdigest()[:32]


def _guess_extension(remote_url: str, content_type: str) -> str:
    try:
        suffix = Path(httpx.URL(remote_url).path).suffix
    except Exception:
        suffix = ""
    if suffix and len(suffix) <= 5:
        return suffix
    return _EXT_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip().lower(), ".jpg")


def cached_path(namespace: str, remote_url: str) -> Path | None:
    """The local file already cached for remote_url in this namespace, if
    there is one — never fetches anything."""
    if not remote_url:
        return None
    directory = _cache_dir(namespace)
    if not directory.exists():
        return None
    matches = list(directory.glob(f"{_cache_key(remote_url)}.*"))
    return matches[0] if matches else None


def get_or_fetch(namespace: str, remote_url: str, timeout: float = 15.0) -> Path | None:
    """The local cached file for remote_url, fetching it into the cache first
    if this is the first time it's been asked for.

    None if there is no URL to fetch, or the fetch fails — a broken or
    expired remote URL should read as "no image available", not as a 500 on
    whatever page tried to show it.
    """
    if not remote_url:
        return None
    existing = cached_path(namespace, remote_url)
    if existing is not None:
        return existing
    try:
        resp = httpx.get(remote_url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except Exception:
        return None
    if not resp.content:
        return None

    directory = _cache_dir(namespace)
    directory.mkdir(parents=True, exist_ok=True)
    ext = _guess_extension(remote_url, resp.headers.get("content-type", ""))
    path = directory / f"{_cache_key(remote_url)}{ext}"
    path.write_bytes(resp.content)
    return path
