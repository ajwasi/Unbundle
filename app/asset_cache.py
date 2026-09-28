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

Fetched lazily — the first time a viewer actually asks to see an image — not
during a sync, which already means potentially thousands of network calls;
adding one more per cover on top of that would be another order of magnitude
of requests for art most of the time nobody scrolls to.

Namespaced (a caller-chosen short string, e.g. "amazon-music-album") purely
to keep different callers' cached files apart on disk — this module knows
nothing about albums, games, or any other domain concept, only URLs.

Hardening notes from review, all addressed here:
- A response is only cached once it looks like an actual image (content-type
  plus a magic-byte sniff) — a transient error page served with a 200 status
  used to get cached forever as if it were the real cover, with no way to
  tell the difference or retry.
- Writes go to a temp file and are atomically renamed into place, so a
  process kill or full disk mid-write can never leave a partial file at the
  final cache path for a later request to pick up.
- A failed fetch is remembered for a short cooldown so a degraded upstream
  doesn't make every page view re-pay a full connect+read timeout for every
  still-uncached image.
- Only http(s) URLs to a non-private host are fetched — the caller's own
  data decides *which* image to fetch, so this is defense in depth, not a
  substitute for callers passing trustworthy URLs. It only catches a literal
  private/loopback IP in the URL string; it does not resolve hostnames or
  guard against DNS rebinding, which would need a real HTTP client with
  connection-level IP pinning to close properly.
"""

import hashlib
import ipaddress
import logging
import os
import re
import time
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
}

# A single float timeout applies separately to each of connect/read/write/pool
# — a slow-connecting or slow-trickling host could otherwise tie up a
# threadpool worker for much longer than the number implies. Read gets the
# most room since a legitimate CDN transfer is the one phase expected to take
# any real time; the rest fail fast.
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# How long a failed fetch is remembered before being retried. In-process only
# (resets on restart) — the cost of one extra retry right after a restart is
# nothing next to what this saves the rest of the time an upstream is down.
_FAILURE_COOLDOWN_SECONDS = 300.0
_recent_failures: dict[str, float] = {}

_PRIVATE_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "0.0.0.0/8",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]

_MAGIC_SIGNATURES = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",
    b"GIF89a",
    b"BM",  # BMP
)


def _looks_like_image(content: bytes) -> bool:
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return True
    return any(content.startswith(sig) for sig in _MAGIC_SIGNATURES)


def _is_fetchable_url(remote_url: str) -> bool:
    """Defense in depth against a URL pointing somewhere it shouldn't — see
    the module docstring for what this does and doesn't catch."""
    try:
        url = httpx.URL(remote_url)
    except Exception:
        return False
    if url.scheme not in ("http", "https") or not url.host:
        return False
    host = url.host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a real hostname — see the docstring's DNS caveat
    return not any(ip in net for net in _PRIVATE_NETWORKS)


_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _cache_dir(namespace: str) -> Path:
    # Every real caller passes a hardcoded literal today, so this can't
    # actually be hit in practice — but this module is explicitly meant to be
    # reused for other domains later, and a namespace built from anything
    # less trusted (or just a typo like "../..") must never be able to escape
    # asset_cache/ on disk. Fail loudly rather than silently sanitizing:
    # silently rewriting a caller's namespace could quietly point it at the
    # wrong directory instead of surfacing the mistake.
    if not _NAMESPACE_RE.match(namespace):
        raise ValueError(f"asset_cache: invalid namespace {namespace!r}")
    return settings.data_dir / "asset_cache" / namespace


def _cache_key(remote_url: str) -> str:
    try:
        url = httpx.URL(remote_url)
        stable = f"{url.host}{url.path}"
    except TypeError:
        stable = remote_url
    return hashlib.sha256(stable.encode()).hexdigest()[:32]


def _guess_extension(remote_url: str, content_type: str) -> str:
    try:
        suffix = Path(httpx.URL(remote_url).path).suffix
    except TypeError:
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
    # The leading-dot temp-file naming in get_or_fetch is deliberate so it
    # can never match this glob — see the comment there.
    matches = sorted(directory.glob(f"{_cache_key(remote_url)}.*"))
    return matches[0] if matches else None


def evict(namespace: str, remote_url: str) -> None:
    """Deletes any cached file for remote_url, if one exists, and clears its
    failure cooldown — the next get_or_fetch call re-fetches from scratch.
    For a cache entry known to be wrong: a poisoned fetch, or the remote
    image genuinely changed under the same URL path.
    """
    path = cached_path(namespace, remote_url)
    if path is not None:
        path.unlink(missing_ok=True)
    _recent_failures.pop(f"{namespace}::{_cache_key(remote_url)}", None)


def get_or_fetch(namespace: str, remote_url: str, timeout: httpx.Timeout | float = _TIMEOUT) -> Path | None:
    """The local cached file for remote_url, fetching it into the cache first
    if this is the first time it's been asked for.

    None if there is no URL to fetch, the URL isn't safe to fetch, the fetch
    fails, or the response doesn't actually look like an image — a broken,
    expired, or unexpected remote response should read as "no image
    available", not as a 500 on whatever page tried to show it, and should
    never be written to the cache as if it were the real thing.
    """
    if not remote_url:
        return None
    existing = cached_path(namespace, remote_url)
    if existing is not None:
        return existing

    key = _cache_key(remote_url)
    failure_key = f"{namespace}::{key}"
    failed_at = _recent_failures.get(failure_key)
    if failed_at is not None and time.monotonic() - failed_at < _FAILURE_COOLDOWN_SECONDS:
        return None

    if not _is_fetchable_url(remote_url):
        logger.warning("asset_cache(%s): refusing to fetch a disallowed URL", namespace)
        _recent_failures[failure_key] = time.monotonic()
        return None

    try:
        resp = httpx.get(remote_url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("asset_cache(%s): fetch failed: %s", namespace, exc)
        _recent_failures[failure_key] = time.monotonic()
        return None

    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if not resp.content or (not content_type.startswith("image/") and not _looks_like_image(resp.content)):
        logger.warning(
            "asset_cache(%s): refusing to cache a non-image response (content-type=%r, %d bytes)",
            namespace,
            content_type,
            len(resp.content),
        )
        _recent_failures[failure_key] = time.monotonic()
        return None

    directory = _cache_dir(namespace)
    directory.mkdir(parents=True, exist_ok=True)
    ext = _guess_extension(remote_url, content_type)
    path = directory / f"{key}{ext}"
    # Leading dot keeps this out of cached_path's "{key}.*" glob, and the
    # write-then-rename means a crash or full disk mid-write can never leave
    # a partial file sitting at the final path for a later request to serve.
    tmp_path = directory / f".{key}.tmp{os.getpid()}"
    tmp_path.write_bytes(resp.content)
    os.replace(tmp_path, path)
    _recent_failures.pop(failure_key, None)
    return path
