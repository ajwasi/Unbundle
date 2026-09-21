"""Refreshes the cached purchased-music inventory.

Auth is derived from the stored Audible login: audible's Authenticator can
mint `.amazon.<tld>` website cookies from its refresh token, and those cookies
were confirmed (2026-09-19) to authenticate against music.amazon.com —
config.json came back with a populated customer id.

The `headers` field a showPurchasedTracks call carries is not assembled from
scratch. An earlier version synthesised it from config.json alone and drew a
bare Tomcat 400 — the endpoint's required subset of its ~twenty x-amzn-*
entries is undocumented, and guessing at it did not work. What does work
(confirmed 2026-09-20, a real sync returned 3.5 MB of JSON) is replaying a
request Amazon's own web player sent, captured once and stored via
app.connectors.amazon_music_template. Every sync starts from that captured
shape; see _session_fields_from_config() and with_fresh_session() for exactly
which parts of it still get refreshed and why the access token alone was not
enough — a template that only refreshed the token got a 200 back with zero
tracks, because the response body was Amazon's own generic error dialog
rather than the real page.

Manual refresh only: this app has no scheduler, and the route is behind the
same rate limiter as every other refresh.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.connectors import amazon_music_connector as amc
from app.connectors import amazon_music_probe as probe
from app.connectors.amazon_music_probe import BASE_HEADERS, cookies_from_audible_credential
from app.connectors.amazon_music_probe import NotConnectedError as _ProbeNotConnectedError
from app.connectors import amazon_music_template as tmpl
from app.models.amazon_music_track import AmazonMusicTrack

# One page is 50 rows (confirmed: multiSelectBar.itemCount). A pause between
# pages keeps a full-library sync from looking like a scrape of an
# undocumented API on a personal account.
PAGE_DELAY_SECONDS = 1.5
# A library this size is already unreasonable; the cap exists so a cursor that
# never terminates cannot loop forever.
MAX_PAGES = 400


class NotConnectedError(Exception):
    """No Audible credential is stored, so there is no Amazon login to use."""


class AmazonMusicRequestError(Exception):
    """Amazon rejected the request itself (a 4xx that is not 401/403), with
    its own message attached. Distinct from an auth error: the credentials
    were fine and the request was not."""


# config.json key -> the x-amzn-* header the API expects. Every target name
# below appears verbatim in the API's own CORS access-control-allow-headers
# list, and every source key appears verbatim in a real config.json — so the
# mapping is read off two confirmed lists rather than guessed.
_CONFIG_TO_HEADER = {
    "deviceId": "x-amzn-device-id",
    "deviceType": "x-amzn-device-type-id",
    "sessionId": "x-amzn-session-id",
    "montanaCsrf": "x-amzn-csrf",
}


def _session_fields_from_config(config: dict) -> dict[str, str]:
    """The session-scoped x-amzn-* headers config.json can supply right now,
    as header name -> value, for splicing into a captured template.

    montanaCsrf and sessionId are bound to the specific browser session that
    produced them — a template that only refreshed the access token got a
    200 back with zero tracks, because the response body was Amazon's own
    generic error dialog rather than the real page. Pairing a captured CSRF
    with this app's own freshly-minted cookies is exactly the mismatched pair
    CSRF protection exists to catch, and Amazon does not surface that as an
    HTTP error.
    """
    fields: dict[str, str] = {}
    for config_key, header_name in _CONFIG_TO_HEADER.items():
        value = config.get(config_key)
        if isinstance(value, str) and value:
            fields[header_name] = value
        elif value:
            # montanaCsrf is an object in some responses; pass it through as
            # JSON rather than str()-ing a dict into something unparseable.
            fields[header_name] = json.dumps(value)
    return fields


def _fetch_config(client) -> dict:
    resp = client.get(amc.config_url())
    if resp.status_code in (401, 403):
        raise amc.AmazonMusicAuthError("Amazon rejected the stored login. Reconnect Audible in Settings.")
    resp.raise_for_status()
    return resp.json()


class NoTemplateError(Exception):
    """No captured request has been saved, so there is no shape to send."""


def _fetch_page(client, base: str, headers_field: str, user_hash: str, cursor: str) -> dict:
    fields = {"headers": headers_field, "sortBy": amc.SORT_RECENTLY_ADDED, "userHash": user_hash}
    if cursor:
        fields["next"] = cursor

    # A JSON body sent under a text/plain Content-Type. Both halves are
    # deliberate and both are read off a real capture:
    #
    #   * JSON, because DevTools rendered the payload as a quoted tree
    #     (sortBy "RECENTLY_ADDED"), which is how it shows JSON — a urlencoded
    #     body renders unquoted. Sending urlencoded produced a bare Tomcat 400,
    #     consistent with the servlet finding no parameters it could parse.
    #   * text/plain, because it is a CORS "simple" type, so the browser skips
    #     the preflight this API's own access-control-allow-methods
    #     (GET, OPTIONS) implies it would otherwise need.
    resp = client.post(
        f"{base}{amc.PURCHASED_TRACKS_PATH}",
        content=json.dumps(fields),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    if resp.status_code in (401, 403):
        raise amc.AmazonMusicAuthError("Amazon rejected the stored login. Reconnect Audible in Settings.")
    if resp.status_code >= 400:
        # Amazon's own complaint is far more useful than "HTTPStatusError",
        # and this is an undocumented API whose failures we have to read to
        # understand. Truncated, and it is the API's error text about the
        # request — not a credential.
        detail = (resp.text or "").strip()[:400]
        raise AmazonMusicRequestError(f"Amazon returned {resp.status_code} for {amc.PURCHASED_TRACKS_PATH}: {detail}")
    resp.raise_for_status()
    return resp.json()


def mark_compilations(db: Session) -> None:
    """An album whose tracks disagree about the artist is a compilation.

    Computed from the inventory rather than by calling showCatalogAlbum once
    per album — it costs no extra requests, and it is only ever used to decide
    a folder name.
    """
    rows = (
        db.query(AmazonMusicTrack.album_asin, AmazonMusicTrack.artist)
        .filter(AmazonMusicTrack.album_asin != "")
        .distinct()
        .all()
    )
    artists_by_album: dict[str, set[str]] = {}
    for album_asin, artist in rows:
        artists_by_album.setdefault(album_asin, set()).add(artist)

    for album_asin, artists in artists_by_album.items():
        is_comp = len({a for a in artists if a}) > 1
        db.query(AmazonMusicTrack).filter(AmazonMusicTrack.album_asin == album_asin).update(
            {"is_compilation": is_comp}, synchronize_session=False
        )
    db.commit()


def upsert_tracks(db: Session, tracks: list[amc.PurchasedTrack], now: datetime | None = None) -> tuple[int, int]:
    """Returns (new, updated). Rows that disappear are flagged by
    flag_missing(), never deleted here."""
    now = now or datetime.utcnow()
    new = updated = 0
    for parsed in tracks:
        row = db.get(AmazonMusicTrack, parsed.track_asin)
        if row is None:
            row = AmazonMusicTrack(track_asin=parsed.track_asin, first_seen_at=now)
            db.add(row)
            new += 1
        else:
            updated += 1
        row.download_id = parsed.download_id
        row.title = parsed.title
        row.artist = parsed.artist
        row.artist_asin = parsed.artist_asin
        row.album = parsed.album
        row.album_asin = parsed.album_asin
        row.duration_display = parsed.duration_display
        row.duration_seconds = parsed.duration_seconds
        if parsed.cover_url:
            # Presigned and expiring — refreshed every sync, and the timestamp
            # is what lets the UI show "stale" instead of a broken image.
            row.cover_url = parsed.cover_url
            row.cover_url_refreshed_at = now
        row.last_seen_at = now
        row.missing_since = None
    db.commit()
    return new, updated


def flag_missing(db: Session, seen_asins: set[str], now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    query = db.query(AmazonMusicTrack).filter(AmazonMusicTrack.missing_since.is_(None))
    if seen_asins:
        query = query.filter(~AmazonMusicTrack.track_asin.in_(seen_asins))
    stale = query.all()
    for row in stale:
        row.missing_since = now
    db.commit()
    return len(stale)


def _diagnose_empty_page(payload: dict) -> dict:
    """A page returned valid JSON but parse_tracks() found nothing in it.

    Rather than report a silent zero, this reuses the same shape-detection
    built for the Settings probe card — find_record_nodes/largest_list_shapes/
    collection_sizes/referenced_endpoints all report key *names*, paths and
    counts, never values, so this is safe to render straight onto the
    Amazon Music page without a second capture-and-paste round trip.
    """
    top_level_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    return {
        "top_level_keys": top_level_keys,
        "collection_sizes": probe.collection_sizes(payload)[:20],
        "record_shapes": probe.find_record_nodes(payload) or probe.largest_list_shapes(payload),
        "endpoints": probe.referenced_endpoints(payload)[:20],
    }


def refresh_purchased_tracks(db: Session) -> dict:
    """Page through the whole purchased library and upsert it.

    Returns a summary dict for the UI. Raises NotConnectedError when there is
    no Audible login to derive cookies from, and AmazonMusicAuthError when
    Amazon rejects what it derives — neither is a 500.
    """
    try:
        cookies = cookies_from_audible_credential(db)
    except _ProbeNotConnectedError as exc:
        # Re-raised under this module's own name so routers import one symbol
        # for "nothing to sync with" rather than reaching into the probe.
        raise NotConnectedError("Audible isn't connected, so there's no Amazon login to sync music with.") from exc

    started = datetime.utcnow()
    seen: set[str] = set()
    new_total = updated_total = 0
    pages = 0

    template = tmpl.load_template(db)
    if template is None:
        raise NoTemplateError(
            "No sync template saved yet. Capture the Purchased view request and save it "
            "in Settings — Amazon's required fields are undocumented, so the only reliable "
            "shape is one its own web player sent."
        )

    with httpx.Client(cookies=cookies, headers=BASE_HEADERS, timeout=30.0, follow_redirects=True) as client:
        config = _fetch_config(client)
        token = config.get("accessToken") or ""
        if not token:
            raise amc.AmazonMusicAuthError(
                "Amazon did not return an access token — the stored Audible login may have expired."
            )
        # The access token and the session-scoped headers (CSRF, session id)
        # all go stale independently of the ~fifteen other captured fields
        # that don't — see with_fresh_session()'s docstring for why refreshing
        # only the token was not enough.
        headers_field = tmpl.with_fresh_session(template.headers_field, token, _session_fields_from_config(config))

        cursor = ""
        diagnostic = None
        candidates_total = 0
        unmatched_shapes: list[list[str]] = []
        while pages < MAX_PAGES:
            payload = _fetch_page(client, amc.api_base(), headers_field, template.user_hash, cursor)
            tracks, candidates, page_unmatched = amc.parse_tracks_with_yield(payload)
            pages += 1
            candidates_total += candidates
            # Kept across pages up to the function's own limit, not reset per
            # page: a shape that recurs on every page is one sample, not
            # dozens of the same thing.
            for shape in page_unmatched:
                if shape not in unmatched_shapes and len(unmatched_shapes) < 5:
                    unmatched_shapes.append(shape)

            if tracks:
                new, updated = upsert_tracks(db, tracks, started)
                new_total += new
                updated_total += updated
                seen.update(t.track_asin for t in tracks)
            elif diagnostic is None:
                # A 200/JSON page that yields no tracks is not the same
                # failure as an auth error or a 4xx — the request worked, but
                # the response didn't match the shape parse_tracks() expects.
                # Captured once (the first such page), not every page, since
                # later pages of the same shape would only repeat it.
                diagnostic = _diagnose_empty_page(payload)

            cursor = amc.parse_next_cursor(payload)
            if not cursor:
                break
            time.sleep(PAGE_DELAY_SECONDS)

    missing = flag_missing(db, seen, started)
    mark_compilations(db)

    result = {
        "pages": pages,
        "tracks_seen": len(seen),
        # Always reported, not just on total failure: a headline track count
        # cannot tell "this library genuinely has few tracks" apart from
        # "most rows are being silently skipped" — a sync that recognised 27
        # of 27 rows and one that recognised 27 of 6,000 both say "27 tracks"
        # unless the denominator is shown too.
        "candidates_seen": candidates_total,
        "new": new_total,
        "updated": updated_total,
        "missing": missing,
        "finished_at": datetime.utcnow(),
    }
    if unmatched_shapes:
        result["unmatched_item_shapes"] = unmatched_shapes
    if not seen and diagnostic is not None:
        result["diagnostic"] = diagnostic
    return result
