"""Reads purchased tracks out of Amazon Music's web-player API.

POST /api/showPurchasedTracks does not return a data document. It returns
Amazon's server-driven UI format: a `methods[].template.widgets[].items[]`
tree describing how to draw the page, where each row is presentation slots
(`primaryText`, `secondaryText1`…) rather than named fields. Everything here
is therefore a *tolerant extraction* from that tree, not a schema mapping.

Three consequences worth stating plainly:

  * This is more fragile than a data API. The slots carry meaning only by
    position, so an Amazon redesign can silently change what `secondaryText2`
    means. Parsing is defensive everywhere and a row that yields no
    download_id is skipped rather than guessed at.
  * Field *locations* below are confirmed against one real capture
    (2026-09-19); field *nesting* is not assumed. download_id and the
    per-row deeplinks are found by searching each row's own subtree, because
    their exact depth was never verified and searching is robust to it.
  * A row's identity is download_id, not the ASIN found under its
    thumbs-up widget. That ASIN looked like the row's own catalogue ASIN in
    the one capture this parser was built against, but a live sync of 10,000
    rows disproved it — it collapsed onto 27 distinct values while
    download_id was distinct on every row. See _find_asin_in_storage_key's
    docstring for the full story; track_asin is kept only as metadata.

Pagination is a cursor named `next`, absent on the first call and carried on
an embedded showPurchasedTracks URL in each response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

import httpx

from app.config import settings

API_HOST = "https://na.web.skill.music.a2z.com"
PURCHASED_TRACKS_PATH = "/api/showPurchasedTracks"
DOWNLOAD_TRACK_PATH = "/api/downloadTrack"
# Confirmed from a real capture (2026-09-27) of https://music.amazon.com/albums/<asin>:
# a generic server-driven-UI renderer, not an album-specific endpoint — see
# amazon_music_sync.sync_album_track_order for the request shape (a "deeplink"
# field naming the page to render, not sortBy/userHash like the paths above).
SHOW_HOME_PATH = "/api/showHome"

# Confirmed value from a real request; the sort dropdown also offers A-Z and
# Z-A. Recently-added is the useful one for an incremental inventory.
SORT_RECENTLY_ADDED = "RECENTLY_ADDED"

# Confirmed value from a real capture (2026-09-27) taken while attempting to
# select an alternate sort in the web player — the exact option clicked isn't
# confirmed, and "NONE" likely means "no explicit sort" rather than literally
# alphabetical. What matters for amazon_music_sync's two-pass strategy isn't
# that meaning, only that it's a *different* ordering criterion than
# RECENTLY_ADDED: if showPurchasedTracks pagination has a result-window limit
# (its behavior matches one, capping out at ~10,000 rows regardless of true
# library size), a differently-ordered pass walks the underlying index in a
# different sequence and can surface rows the first pass's window never
# reached, purely by not stopping in the same place.
SORT_NONE = "NONE"

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
    """An ASIN carried as the thumbs-up widget's storage key (storageGroup
    TRACK_RATINGS) — kept as informational metadata only, NOT as identity.

    It was assumed to be the row's own catalogue ASIN because that held in
    one capture, but a live sync disproved it: 10,000 rows produced only 27
    distinct values here, while download_id (see _find_download_id) was
    distinct on every single one. Whatever this value actually keys off —
    the album is the leading guess, never confirmed — it repeats across many
    rows of the same purchased library, so parse_track_item() no longer
    requires it and PurchasedTrack.track_asin is no longer anyone's identity.
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
    """One rendered row -> one track, or None when it carries no download_id.

    download_id, not the storage-key ASIN, is the row's real identity — see
    _find_asin_in_storage_key's docstring for how that was confirmed. A row
    without a download_id has no stable identity to key on, so it is dropped
    rather than stored under a guessed key.
    """
    download_id = _find_download_id(item)
    if not download_id:
        return None

    duration_display = (item.get("secondaryText3") or "").strip()
    return PurchasedTrack(
        track_asin=_find_asin_in_storage_key(item),
        download_id=download_id,
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
    did not produce a download_id: whether those are real track rows this
    parser doesn't yet handle, or genuinely non-track widgets (section
    headers, ads, rails) that should be skipped, is exactly what a redesign
    would change and exactly what a bare "N tracks synced" cannot
    distinguish.

    Deduplicates by download_id, not track_asin: an earlier version deduped
    by the storage-key ASIN on the assumption that it was the row's own
    identity, and a live sync disproved that outright — 10,000 rows
    collapsed onto 27 ASINs while every single one carried its own distinct
    download_id. See _find_asin_in_storage_key's and parse_track_item's
    docstrings for the full story.

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
                if track.download_id not in seen:
                    seen.add(track.download_id)
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


def find_signed_download_url(payload: Any) -> str:
    """The presigned CloudFront delivery URL downloadTrack's response embeds.

    Confirmed shape, from a real capture: a d*.cloudfront.net host, path
    .../DigitalMusicDeliveryService/..., query params including cdoid (the
    same download_id sent in the request) and isrc. downloadTrack is known
    to return this "under a key like 'url'", but the exact wrapping key was
    only ever eyeballed, never pinned down structurally — searched by the
    URL's own unmistakable shape instead of a key name, so this does not
    silently break if that key turns out to be named something else.

    Checked by actually parsing each candidate and inspecting its real host
    and query parameters, not by searching the raw string for "cloudfront.net"
    and "cdoid=": a substring match there is spoofable by any string that
    merely contains those characters somewhere (a query value, a path
    segment, an unrelated host like "cloudfront.net.evil.example") without
    being a real CloudFront URL at all — this result is used to open an
    outbound connection, so it has to be the real host, not a lookalike.
    """
    for text in _strings(payload):
        if not text.startswith("http"):
            continue
        try:
            url = httpx.URL(text)
        except Exception:
            continue
        host = url.host or ""
        if (host == "cloudfront.net" or host.endswith(".cloudfront.net")) and "cdoid" in url.params:
            return text
    return ""


def parse_delivery_url_metadata(delivery_url: str) -> dict:
    """ISRC and purchase timestamp, read off the signed delivery URL
    downloadTrack returns — confirmed real query params (isrc, pt) from the
    same capture find_signed_download_url's own docstring cites:
    ?e=...&cid=...&cdoid=...&isrc=GBAAM8300001&tid=...&pt=1566309461277&h=...

    Neither value is available anywhere else in this connector —
    showPurchasedTracks carries neither — so this is the only source, and
    only ever populated for a track once it has actually been downloaded at
    least once. `pt` is 13 digits in the real example, consistent with epoch
    milliseconds (dividing by 1000 lands on a plausible 2019 purchase date);
    `e`, by contrast, is 10 digits in that same example — epoch seconds, and
    the URL's own expiry, not a purchase date — the two are easy to mix up
    from digit count alone if read too quickly.
    """
    try:
        url = httpx.URL(delivery_url)
    except Exception:
        return {}
    isrc = url.params.get("isrc") or ""
    pt = url.params.get("pt") or ""
    purchased_at = None
    if pt.isdigit():
        try:
            purchased_at = datetime.utcfromtimestamp(int(pt) / 1000)
        except (ValueError, OSError, OverflowError):
            purchased_at = None
    return {"isrc": isrc, "purchased_at": purchased_at}


def parse_catalog_album_track_titles(payload: dict) -> list[str]:
    """Ordered track titles from a catalog album-detail response (POST
    /api/showHome with a deeplink of /albums/<asin>), confirmed from a real
    capture (2026-09-27).

    There is no explicit track-number field anywhere in this response —
    order is carried purely by position in the track table's own `items`
    array, which is why this returns a plain ordered list of titles rather
    than (title, number) pairs; the caller assigns 1-based positions itself
    from list position, and matches titles back to its own already-synced
    rows (track_asin was disproven as a reliable per-track key already, so
    it is not used for matching here either — see that field's own
    docstring on AmazonMusicTrack).

    Found by walking for the DescriptiveRowItemElement interface
    specifically, not just any "items" list like parse_tracks_with_yield
    does for the purchased-library feed: a catalog album page's response
    also embeds unrelated item lists (site-wide navigation, a "new
    releases" activity feed for followed artists) using different
    interfaces entirely, and only this one names an actual track row.
    """
    titles = []
    for node in _walk(payload):
        if isinstance(node, dict) and str(node.get("interface", "")).endswith("DescriptiveRowItemElement"):
            title = node.get("primaryText")
            if isinstance(title, str) and title:
                titles.append(title)
    return titles
