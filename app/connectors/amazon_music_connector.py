"""Reads purchased tracks out of Amazon Music's web-player API.

POST /api/showPurchasedTracks does not return a data document. It returns
Amazon's server-driven UI format: a `methods[].template.widgets[].items[]`
tree describing how to draw the page, where each row is presentation slots
(`primaryText`, `secondaryText1`…) rather than named fields. Everything here
is therefore a *tolerant extraction* from that tree, not a schema mapping.

Two consequences worth stating plainly:

  * This is more fragile than a data API. The slots carry meaning only by
    position, so an Amazon redesign can silently change what `secondaryText2`
    means. Parsing is defensive everywhere and a row that yields no ASIN is
    skipped rather than guessed at.
  * Field *locations* below are confirmed against one real capture
    (2026-09-19); field *nesting* is not assumed. The track ASIN and the
    per-row deeplinks are found by searching each row's own subtree, because
    their exact depth was never verified and searching is robust to it.

Pagination is a cursor named `next`, absent on the first call and carried on
an embedded showPurchasedTracks URL in each response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from app.config import settings

API_HOST = "https://na.web.skill.music.a2z.com"
PURCHASED_TRACKS_PATH = "/api/showPurchasedTracks"
DOWNLOAD_TRACK_PATH = "/api/downloadTrack"

# Confirmed value from a real request; the sort dropdown also offers A-Z and
# Z-A. Recently-added is the useful one for an incremental inventory.
SORT_RECENTLY_ADDED = "RECENTLY_ADDED"

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
ALBUM_LINK_RE = re.compile(r"/albums/([A-Z0-9]{10})")
ARTIST_LINK_RE = re.compile(r"/artists/([A-Z0-9]{10})")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
DURATION_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


def api_base() -> str:
    """settings.demo_mode points every call at mock_api instead — same switch
    the Humble, Steam and GOG connectors already use."""
    return f"{settings.mock_api_base_url}/amazon-music" if settings.demo_mode else API_HOST


def config_url() -> str:
    """Session state (access token, csrf, device/customer ids) lives on the
    player origin, not the API host — hence a separate base from api_base()."""
    if settings.demo_mode:
        return f"{settings.mock_api_base_url}/amazon-music/config.json"
    return "https://music.amazon.com/config.json"


class AmazonMusicAuthError(Exception):
    """Stored credentials were rejected — the caller surfaces a reconnect
    prompt rather than a 500."""


@dataclass
class PurchasedTrack:
    track_asin: str
    download_id: str
    title: str
    artist: str
    artist_asin: str
    album: str
    album_asin: str
    duration_display: str
    duration_seconds: int | None
    cover_url: str


def _walk(node: Any) -> Iterator[Any]:
    """Every value in the tree, depth-first."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _strings(node: Any) -> Iterator[str]:
    for value in _walk(node):
        if isinstance(value, str):
            yield value


def parse_duration(display: str) -> int | None:
    """"3:57" -> 237, "1:02:03" -> 3723. None when it isn't a duration at all.

    Returns None rather than guessing: secondaryText3 is a display slot whose
    meaning is positional, so a row carrying something else there must not
    silently become a bogus number.
    """
    match = DURATION_RE.match(display.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def _find_asin_in_storage_key(item: dict) -> str:
    """The track's catalogue ASIN, carried as the thumbs-up widget's storage
    key (storageGroup TRACK_RATINGS). Searched rather than indexed by path:
    its exact nesting under `button.observer` is confirmed once, in one
    capture, and a sibling widget could just as well carry it tomorrow.
    """
    for node in _walk(item):
        if isinstance(node, dict):
            key = node.get("storageKey")
            if isinstance(key, str) and ASIN_RE.match(key):
                return key
    return ""


def _find_download_id(item: dict) -> str:
    """The per-library-object UUID that /api/downloadTrack takes as `id`.

    It is a *key name* under onCheckboxSelected.states, not a value, which is
    why this looks at dict keys rather than reading a field.
    """
    for node in _walk(item):
        if isinstance(node, dict):
            for key in node:
                if UUID_RE.match(key):
                    return key
    return ""


def _find_link_asin(item: dict, pattern: re.Pattern[str]) -> str:
    for text in _strings(item):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def _find_cover_url(item: dict) -> str:
    """Presigned S3 image URL. Expires — see AmazonMusicTrack.cover_url."""
    image = item.get("image")
    if isinstance(image, str) and image.startswith("http"):
        return image
    for text in _strings(item):
        if text.startswith("http") and (".jpg" in text or ".png" in text):
            return text
    return ""


def parse_track_item(item: dict) -> PurchasedTrack | None:
    """One rendered row -> one track, or None when it carries no ASIN.

    A row without an ASIN has no stable identity to key on, so it is dropped
    rather than stored under a guessed key.
    """
    asin = _find_asin_in_storage_key(item)
    if not asin:
        return None

    duration_display = (item.get("secondaryText3") or "").strip()
    return PurchasedTrack(
        track_asin=asin,
        download_id=_find_download_id(item),
        title=(item.get("primaryText") or "").strip(),
        artist=(item.get("secondaryText1") or "").strip(),
        artist_asin=_find_link_asin(item, ARTIST_LINK_RE),
        album=(item.get("secondaryText2") or "").strip(),
        album_asin=_find_link_asin(item, ALBUM_LINK_RE),
        duration_display=duration_display,
        duration_seconds=parse_duration(duration_display),
        cover_url=_find_cover_url(item),
    )


def parse_tracks_with_yield(
    payload: dict, unmatched_limit: int = 5
) -> tuple[list[PurchasedTrack], int, list[list[str]]]:
    """Same extraction as parse_tracks(), plus the two things that make a
    *partial* yield legible instead of silent.

    A headline track count alone cannot tell "this account genuinely has few
    tracks" apart from "most rows are being silently skipped" — both look
    identical from the outside. `candidates` is every item dict this walk
    actually examined, matched or not, so it and the track count together
    show the real yield ratio. `unmatched_shapes` is the key *names* — never
    values — of up to `unmatched_limit` distinct shapes among the items that
    did not produce an ASIN: whether those are real track rows this parser
    doesn't yet handle, or genuinely non-track widgets (section headers, ads,
    rails) that should be skipped, is exactly what a redesign would change
    and exactly what a bare "N tracks synced" cannot distinguish.

    Finds `items` lists by walking rather than by the confirmed path
    methods[].template.widgets[].items[], so an extra wrapper level in a
    future response shape doesn't empty the sync silently.
    """
    tracks: list[PurchasedTrack] = []
    seen: set[str] = set()
    candidates = 0
    unmatched_shapes: list[list[str]] = []

    for node in _walk(payload):
        if not isinstance(node, dict):
            continue
        items = node.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            candidates += 1
            track = parse_track_item(item)
            if track:
                if track.track_asin not in seen:
                    seen.add(track.track_asin)
                    tracks.append(track)
            elif len(unmatched_shapes) < unmatched_limit:
                shape = sorted(item.keys())
                if shape not in unmatched_shapes:
                    unmatched_shapes.append(shape)

    return tracks, candidates, unmatched_shapes


def parse_tracks(payload: dict) -> list[PurchasedTrack]:
    """Every row in the response, wherever the widget tree puts it.

    Thin wrapper over parse_tracks_with_yield() for callers that only need
    the tracks — parsing tests and the download-side code, say — without
    also carrying the yield-diagnostic numbers.
    """
    tracks, _candidates, _unmatched = parse_tracks_with_yield(payload)
    return tracks


def parse_next_cursor(payload: dict) -> str:
    """The opaque `next` token for the following page, read off the
    showPurchasedTracks URL the response embeds. Empty means last page.
    """
    for text in _strings(payload):
        if PURCHASED_TRACKS_PATH not in text or not text.startswith("http"):
            continue
        try:
            url = httpx.URL(text)
        except Exception:
            continue
        token = url.params.get("next")
        if token:
            return token
    return ""
