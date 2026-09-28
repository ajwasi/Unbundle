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

What this module protects against, precisely (from two rounds of review):
- A response that isn't actually an image (an error/login page served with a
  200 status) is never cached — checked by content-type plus a magic-byte
  sniff of the body, and the body is streamed with an early size cutoff
  rather than buffered unconditionally first.
- Writes go to a temp file and are atomically renamed into place, and any
  OS-level failure during that (a full disk, a permissions error) is treated
  as a normal fetch failure rather than an unhandled exception.
- A failed fetch is remembered for a cooldown instead of being retried (and
  re-paying a full timeout) on every request while an upstream is degraded;
  that bookkeeping is itself bounded and lock-protected so it can't grow
  forever or race under concurrent requests.
- Only http(s) URLs to a non-private host are fetched, and — since a server
  at an otherwise-"safe" host could just redirect somewhere it shouldn't —
  every redirect hop is re-validated the same way before being followed,
  not just the URL the caller originally passed in.
- What this does NOT do: resolve hostnames to check for DNS rebinding (a
  real hostname that currently resolves to a public IP is trusted as-is;
  only a literal private/loopback IP already in the URL is caught), and it
  doesn't second-guess *which* URL a caller asks it to fetch — that's the
  caller's own data to trust, this is defense in depth on top of it.
"""

import contextlib
import hashlib
import ipaddress
import logging
import os
import re
import threading
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
}

# SVG is deliberately not supported: it's the one common image format that
# can embed <script>, and while today's exclusively-<img> usage is safe
# (browsers don't execute scripts in an image-context SVG), this module is
# explicitly meant to be reused by other callers later, with no guarantee
# they'd all serve it the same safe way. Rejected even when content-type
# correctly claims it, not just left off the extension map.
_DISALLOWED_CONTENT_TYPES = {"image/svg+xml"}

# A single float timeout applies separately to each of connect/read/write/pool
# — a slow-connecting or slow-trickling host could otherwise tie up a
# threadpool worker for much longer than the number implies. Read gets the
# most room since a legitimate CDN transfer is the one phase expected to take
# any real time; the rest fail fast.
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# Cover art is small; anything past this is either a mistake or something
# this cache shouldn't be buffering into memory at all. Enforced by
# streaming and aborting early, not by checking Content-Length (a
# server can omit or lie about that) or by capping after the fact.
_MAX_RESPONSE_BYTES = 25 * 1024 * 1024

_MAX_REDIRECT_HOPS = 5

# One persistent, reused client rather than the top-level httpx.get()
# convenience function — get() opens and tears down its own connection for
# every single call, which is fine for a one-off script but means a fresh
# TCP+TLS handshake per fetch in a loop (a warm-up run, in particular) when
# there's no reason not to reuse a connection across calls to the same host.
# follow_redirects is off deliberately: see _fetch_validated below.
_client = httpx.Client(follow_redirects=False)


class _BlockedURLError(httpx.HTTPError):
    """Raised in place of actually fetching, for a URL (or redirect target)
    _is_fetchable_url refused — a subclass of httpx.HTTPError so it's caught
    by the same handling as a real network/status failure, with an honest
    name instead of misusing an unrelated httpx exception to mean this."""


class _ResponseTooLargeError(httpx.HTTPError):
    """Raised when a response body exceeds _MAX_RESPONSE_BYTES, again as an
    httpx.HTTPError subclass so it flows through the same failure path."""


# How long a failed fetch is remembered before being retried. In-process only
# (resets on restart) — the cost of one extra retry right after a restart is
# nothing next to what this saves the rest of the time an upstream is down.
_FAILURE_COOLDOWN_SECONDS = 300.0

# Caps _recent_failures' own growth — a library with enough covers that are
# permanently gone (not just temporarily down) would otherwise grow this
# dict forever, since each retry-and-fail-again just refreshes that entry's
# own timestamp rather than ever aging out unprompted.
_MAX_FAILURE_ENTRIES = 2000

_recent_failures: dict[str, float] = {}
# Guards _recent_failures — get_or_fetch runs on FastAPI's sync threadpool,
# so concurrent requests for the same not-yet-cached or not-yet-failed URL
# are a real possibility, not a hypothetical; without this, two threads can
# race the check-then-write on this dict (wasted duplicate fetches, not
# corruption, but real waste worth just... not having).
_failures_lock = threading.Lock()


def _record_failure(failure_key: str) -> None:
    with _failures_lock:
        _recent_failures[failure_key] = time.monotonic()
        if len(_recent_failures) > _MAX_FAILURE_ENTRIES:
            # Drop the oldest third in one pass rather than trimming to the
            # cap one entry at a time on every single failure once it's full.
            oldest = sorted(_recent_failures.items(), key=lambda kv: kv[1])
            for key, _ts in oldest[: _MAX_FAILURE_ENTRIES // 3]:
                _recent_failures.pop(key, None)


def _in_cooldown(failure_key: str) -> bool:
    with _failures_lock:
        failed_at = _recent_failures.get(failure_key)
    return failed_at is not None and time.monotonic() - failed_at < _FAILURE_COOLDOWN_SECONDS


def _clear_failure(failure_key: str) -> None:
    with _failures_lock:
        _recent_failures.pop(failure_key, None)


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

# BM (2 bytes) — BMP's own full magic number, no coding mistake here, the
# format just doesn't have a longer one — is deliberately excluded from this
# list even though the extension map still supports BMP: two bytes is weak
# enough that plenty of non-image content could satisfy it by coincidence,
# and unlike the others this signature can't be strengthened. A BMP is still
# accepted, just only via a correct content-type header, never via this
# fallback sniff alone.
_MAGIC_SIGNATURES = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",
    b"GIF89a",
)


def _looks_like_image(content: bytes) -> bool:
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return True
    if content[4:8] == b"ftyp" and content[8:12] in (b"avif", b"avis"):
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
    # An IPv4-mapped IPv6 literal (::ffff:127.0.0.1) parses as an
    # IPv6Address, which is never "in" any of the plain IPv4Network entries
    # below by construction — checked and unwrapped separately, or a
    # loopback/private address written this way sails straight through.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
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


def _safe_log_url(remote_url: str) -> str:
    """host+path only, no query string — a presigned URL's signature lives
    in the query string, and this whole module exists to stop that signature
    from being exposed anywhere it doesn't need to be. A raw exception's own
    str() often embeds the full request URL (httpx.HTTPStatusError does, for
    one) — never log that directly; log this instead.
    """
    try:
        url = httpx.URL(remote_url)
        return f"{url.scheme}://{url.host}{url.path}"
    except Exception:
        return "(unparseable URL)"


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


def evict(namespace: str, remote_url: str, reason: str = "") -> None:
    """Deletes any cached file for remote_url, if one exists, and clears its
    failure cooldown — the next get_or_fetch call re-fetches from scratch.
    For a cache entry known to be wrong: a poisoned fetch, or the remote
    image genuinely changed under the same URL path.

    reason is optional, freeform, logged at info level if given — purely for
    telling "a user asked to force a refresh" apart from "our own validation
    rejected the last fetch" later, in whatever's reading the logs. Doesn't
    change behavior either way.
    """
    if reason:
        logger.info("asset_cache(%s): evicting %s (%s)", namespace, _safe_log_url(remote_url), reason)
    path = cached_path(namespace, remote_url)
    if path is not None:
        path.unlink(missing_ok=True)
    _clear_failure(f"{namespace}::{_cache_key(remote_url)}")


def _fetch_validated(url: str, timeout: httpx.Timeout | float) -> tuple[bytes, str]:
    """GETs url, following redirects manually (one hop at a time, up to
    _MAX_REDIRECT_HOPS) so each hop is re-validated against
    _is_fetchable_url before being followed — the client's own
    follow_redirects=True would otherwise only ever validate the URL the
    caller originally passed in, so a "safe" host could redirect straight to
    a private address with nothing to stop it.

    Streams the final response rather than buffering it unconditionally,
    aborting with _ResponseTooLargeError as soon as _MAX_RESPONSE_BYTES is
    exceeded. Returns (content, content_type) on success; raises
    httpx.HTTPError (or a subclass) for every failure mode, so the caller
    has exactly one thing to catch.
    """
    for _ in range(_MAX_REDIRECT_HOPS):
        if not _is_fetchable_url(url):
            raise _BlockedURLError(f"refusing to fetch {_safe_log_url(url)}")
        with _client.stream("GET", url, timeout=timeout) as resp:
            if resp.is_redirect and resp.has_redirect_location:
                url = str(httpx.URL(url).join(resp.headers["location"]))
                continue
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            chunks = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    raise _ResponseTooLargeError(f"{_safe_log_url(url)} exceeded {_MAX_RESPONSE_BYTES} bytes")
                chunks.append(chunk)
            return b"".join(chunks), content_type
    raise httpx.TooManyRedirects(f"exceeded {_MAX_REDIRECT_HOPS} redirects fetching {_safe_log_url(url)}")


def get_or_fetch(namespace: str, remote_url: str, timeout: httpx.Timeout | float = _TIMEOUT) -> Path | None:
    """The local cached file for remote_url, fetching it into the cache first
    if this is the first time it's been asked for.

    None if there is no URL to fetch, the URL (or a redirect it leads to)
    isn't safe to fetch, the fetch fails, or the response doesn't actually
    look like an image — a broken, expired, or unexpected remote response
    should read as "no image available", not as a 500 on whatever page
    tried to show it, and should never be written to the cache as if it
    were the real thing.
    """
    if not remote_url:
        return None
    existing = cached_path(namespace, remote_url)
    if existing is not None:
        return existing

    key = _cache_key(remote_url)
    failure_key = f"{namespace}::{key}"
    if _in_cooldown(failure_key):
        return None

    try:
        content, content_type = _fetch_validated(remote_url, timeout)
    except httpx.HTTPError as exc:
        logger.warning(
            "asset_cache(%s): fetch failed for %s (%s)", namespace, _safe_log_url(remote_url), type(exc).__name__
        )
        _record_failure(failure_key)
        return None

    if not content or content_type in _DISALLOWED_CONTENT_TYPES:
        looks_ok = False
    else:
        looks_ok = content_type.startswith("image/") or _looks_like_image(content)
    if not looks_ok:
        logger.warning(
            "asset_cache(%s): refusing to cache a non-image response for %s (content-type=%r, %d bytes)",
            namespace,
            _safe_log_url(remote_url),
            content_type,
            len(content),
        )
        _record_failure(failure_key)
        return None

    directory = _cache_dir(namespace)
    directory.mkdir(parents=True, exist_ok=True)
    ext = _guess_extension(remote_url, content_type)
    path = directory / f"{key}{ext}"
    # Leading dot keeps this out of cached_path's "{key}.*" glob, and the
    # write-then-rename means a crash or full disk mid-write can never leave
    # a partial file sitting at the final path for a later request to serve.
    tmp_path = directory / f".{key}.tmp{os.getpid()}"
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning(
            "asset_cache(%s): failed to write cache file for %s (%s)",
            namespace,
            _safe_log_url(remote_url),
            type(exc).__name__,
        )
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        _record_failure(failure_key)
        return None

    _clear_failure(failure_key)
    return path
