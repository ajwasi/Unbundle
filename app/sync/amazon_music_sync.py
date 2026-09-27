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
shape; see probe.session_fields_from_config() and with_fresh_session() for exactly
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
from app.models.amazon_music_album_catalog_sync import AmazonMusicAlbumCatalogSync
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


def _fetch_config(client) -> dict:
    resp = client.get(amc.config_url())
    if resp.status_code in (401, 403):
        raise amc.AmazonMusicAuthError("Amazon rejected the stored login. Reconnect Audible in Settings.")
    resp.raise_for_status()
    return resp.json()


class NoTemplateError(Exception):
    """No captured request has been saved, so there is no shape to send."""


def _fetch_page(client, base: str, headers_field: str, user_hash: str, cursor: str, sort_by: str = amc.SORT_RECENTLY_ADDED) -> dict:
    fields = {"headers": headers_field, "sortBy": sort_by, "userHash": user_hash}
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
        row = db.get(AmazonMusicTrack, parsed.download_id)
        if row is None:
            row = AmazonMusicTrack(download_id=parsed.download_id, first_seen_at=now)
            db.add(row)
            new += 1
        else:
            updated += 1
        row.track_asin = parsed.track_asin
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


def flag_missing(db: Session, seen_ids: set[str], now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    query = db.query(AmazonMusicTrack).filter(AmazonMusicTrack.missing_since.is_(None))
    if seen_ids:
        query = query.filter(~AmazonMusicTrack.download_id.in_(seen_ids))
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


def _authenticate(db: Session) -> tuple[dict, tmpl.SyncTemplate]:
    """Cookies plus the captured request template — the two prerequisites
    every call into Amazon Music's API needs, shared by the purchased-library
    sync and the on-demand per-album track-order fetch alike. Raises
    NotConnectedError/NoTemplateError, which both callers handle identically.
    """
    try:
        cookies = cookies_from_audible_credential(db)
    except _ProbeNotConnectedError as exc:
        # Re-raised under this module's own name so routers import one symbol
        # for "nothing to sync with" rather than reaching into the probe.
        raise NotConnectedError("Audible isn't connected, so there's no Amazon login to sync music with.") from exc

    template = tmpl.load_template(db)
    if template is None:
        raise NoTemplateError(
            "No sync template saved yet. Capture the Purchased view request and save it "
            "in Settings — Amazon's required fields are undocumented, so the only reliable "
            "shape is one its own web player sent."
        )
    return cookies, template


def _fresh_headers_field(client, template: tmpl.SyncTemplate) -> str:
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
    return tmpl.with_fresh_session(template.headers_field, token, probe.session_fields_from_config(config))


def _page_through(client, headers_field: str, user_hash: str, sort_by: str, db: Session, started: datetime) -> dict:
    """Pages through one full sortBy ordering, upserting as it goes.

    A single ordering's pagination has been observed to stop cleanly (a page
    with no `next` cursor, no error) at around 10,000 rows on a library
    confirmed larger than that — the classic signature of a search-index
    result-window limit (Elasticsearch's default is exactly 10,000), not a
    bug in this loop. Called twice by refresh_purchased_tracks with two
    different, both confirmed-real sortBy values for exactly that reason: a
    differently-ordered pass walks whatever index backs this endpoint in a
    different sequence, and can reach rows the first pass's window never did,
    purely by not stopping in the same place. Every row upserted here still
    goes through upsert_tracks' own download_id-based identity, so a row both
    passes reach is just an "updated" the second time, never a duplicate.
    """
    seen: set[str] = set()
    new_total = updated_total = 0
    pages = 0
    candidates_total = 0
    unmatched_shapes: list[list[str]] = []
    diagnostic = None
    cursor = ""
    while pages < MAX_PAGES:
        payload = _fetch_page(client, amc.api_base(), headers_field, user_hash, cursor, sort_by)
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
            seen.update(t.download_id for t in tracks)
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

    return {
        "seen": seen,
        "new": new_total,
        "updated": updated_total,
        "pages": pages,
        "candidates": candidates_total,
        "unmatched_shapes": unmatched_shapes,
        "diagnostic": diagnostic,
    }


def refresh_purchased_tracks(db: Session) -> dict:
    """Page through the whole purchased library, twice, and upsert it.

    Returns a summary dict for the UI. Raises NotConnectedError when there is
    no Audible login to derive cookies from, and AmazonMusicAuthError when
    Amazon rejects what it derives — neither is a 500.
    """
    cookies, template = _authenticate(db)
    started = datetime.utcnow()

    with httpx.Client(cookies=cookies, headers=BASE_HEADERS, timeout=30.0, follow_redirects=True) as client:
        headers_field = _fresh_headers_field(client, template)

        first = _page_through(client, headers_field, template.user_hash, amc.SORT_RECENTLY_ADDED, db, started)
        second = _page_through(client, headers_field, template.user_hash, amc.SORT_NONE, db, started)

    seen = first["seen"] | second["seen"]
    # How many tracks only the second pass ever reached — the number that
    # actually tells you whether the two-pass strategy did anything, versus
    # just reporting a track count with no way to see that.
    second_pass_only = len(second["seen"] - first["seen"])

    missing = flag_missing(db, seen, started)
    mark_compilations(db)

    unmatched_shapes: list[list[str]] = []
    for shape in first["unmatched_shapes"] + second["unmatched_shapes"]:
        if shape not in unmatched_shapes and len(unmatched_shapes) < 5:
            unmatched_shapes.append(shape)

    result = {
        "pages": first["pages"] + second["pages"],
        "tracks_seen": len(seen),
        # Always reported, not just on total failure: a headline track count
        # cannot tell "this library genuinely has few tracks" apart from
        # "most rows are being silently skipped" — a sync that recognised 27
        # of 27 rows and one that recognised 27 of 6,000 both say "27 tracks"
        # unless the denominator is shown too.
        "candidates_seen": first["candidates"] + second["candidates"],
        "new": first["new"] + second["new"],
        "updated": first["updated"] + second["updated"],
        "missing": missing,
        "second_pass_only_tracks": second_pass_only,
        "finished_at": datetime.utcnow(),
    }
    if unmatched_shapes:
        result["unmatched_item_shapes"] = unmatched_shapes
    if not seen and (first["diagnostic"] or second["diagnostic"]):
        result["diagnostic"] = first["diagnostic"] or second["diagnostic"]
    return result


def sync_album_track_order(db: Session, album_asin: str) -> dict:
    """On demand only, never part of refresh_purchased_tracks: fetch one
    album's real tracklist from Amazon's catalog-browsing page (POST
    /api/showHome with a deeplink of /albums/<asin>) and use its order to
    number this app's own already-synced tracks for that album.

    A genuinely different request from showPurchasedTracks — confirmed from a
    real capture (2026-09-27) of https://music.amazon.com/albums/<asin> — not
    a variant of it: the body is {"deeplink": ..., "headers": ...} with no
    sortBy/userHash, and the response is Amazon's general catalog template,
    not the purchased-library one. Deliberately not folded into the regular
    sync: that already makes two full passes over the whole library, and
    hitting this per-album endpoint for every album on top of that would be
    one more request per album for data most of the time nobody looks at.

    Matches by exact title within the album — track_asin was disproven as a
    reliable per-track identity already (see AmazonMusicTrack's own
    docstring), so it isn't trusted for this matching either, and this
    catalog response carries no download_id at all to match on instead.
    A track whose title doesn't appear in this fetch keeps no track_number
    (cleared first, so a track dropped from a reissue's listing doesn't keep
    a stale number from a previous, different fetch).
    """
    cookies, template = _authenticate(db)

    with httpx.Client(cookies=cookies, headers=BASE_HEADERS, timeout=30.0, follow_redirects=True) as client:
        headers_field = _fresh_headers_field(client, template)

        deeplink_field = json.dumps(
            {"interface": "DeeplinkInterface.v1_0.DeeplinkClientInformation", "deeplink": f"/albums/{album_asin}"}
        )
        resp = client.post(
            f"{amc.api_base()}{amc.SHOW_HOME_PATH}",
            content=json.dumps({"deeplink": deeplink_field, "headers": headers_field}),
            headers={"Content-Type": "text/plain;charset=UTF-8"},
        )
        if resp.status_code in (401, 403):
            raise amc.AmazonMusicAuthError("Amazon rejected the stored login. Reconnect Audible in Settings.")
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()[:400]
            raise AmazonMusicRequestError(f"Amazon returned {resp.status_code} for {amc.SHOW_HOME_PATH}: {detail}")
        resp.raise_for_status()
        payload = resp.json()

    titles = amc.parse_catalog_album_track_titles(payload)

    rows = db.query(AmazonMusicTrack).filter(AmazonMusicTrack.album_asin == album_asin).all()
    by_title: dict[str, list[AmazonMusicTrack]] = {}
    for row in rows:
        by_title.setdefault(row.title, []).append(row)
        row.track_number = None

    matched = 0
    for position, title in enumerate(titles, start=1):
        for row in by_title.get(title, []):
            row.track_number = position
            matched += 1

    sync_row = db.get(AmazonMusicAlbumCatalogSync, album_asin)
    if sync_row is None:
        sync_row = AmazonMusicAlbumCatalogSync(album_asin=album_asin)
        db.add(sync_row)
    sync_row.synced_at = datetime.utcnow()
    sync_row.tracks_found = len(titles)
    sync_row.tracks_matched = matched
    db.commit()

    return {"tracks_found": len(titles), "tracks_matched": matched, "total_tracks": len(rows)}
