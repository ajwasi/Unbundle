"""Refreshes the cached purchased-music inventory.

Auth is derived from the stored Audible login: audible's Authenticator can
mint `.amazon.<tld>` website cookies from its refresh token, and those cookies
were confirmed (2026-09-19) to authenticate against music.amazon.com —
config.json came back with a populated customer id.

ONE PIECE HERE IS INFERRED, NOT CONFIRMED. Two facts are established:
config.json returns an `accessToken`, and a real captured request carries its
auth in a form field named `headers` holding
`{"x-amzn-authentication": {"interface": …, "accessToken": …}}`. That those
two connect — that the token from config.json is accepted in that field — has
never been tested end to end, because doing so requires a live authenticated
call. If a refresh fails with an auth error, this assembly is the first thing
to suspect, not the cookies.

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
from app.connectors.amazon_music_probe import BASE_HEADERS, cookies_from_audible_credential
from app.connectors.amazon_music_probe import NotConnectedError as _ProbeNotConnectedError
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


def _auth_headers_field(config: dict) -> str:
    """Build the `headers` form field a web-player call carries.

    Shape copied verbatim from a real captured request; the token is read from
    config.json. See this module's docstring on why this is the inferred step.
    """
    token = config.get("accessToken") or ""
    if not token:
        raise amc.AmazonMusicAuthError("Amazon did not return an access token — the stored login may have expired.")
    return json.dumps(
        {
            "x-amzn-authentication": json.dumps(
                {
                    "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement",
                    "accessToken": token,
                }
            )
        }
    )


def _fetch_config(client) -> dict:
    resp = client.get(amc.config_url())
    if resp.status_code in (401, 403):
        raise amc.AmazonMusicAuthError("Amazon rejected the stored login. Reconnect Audible in Settings.")
    resp.raise_for_status()
    return resp.json()


def _fetch_page(client, base: str, headers_field: str, cursor: str) -> dict:
    data = {"headers": headers_field, "sortBy": amc.SORT_RECENTLY_ADDED, "userHash": "{}"}
    if cursor:
        data["next"] = cursor
    resp = client.post(f"{base}{amc.PURCHASED_TRACKS_PATH}", data=data)
    if resp.status_code in (401, 403):
        raise amc.AmazonMusicAuthError("Amazon rejected the stored login. Reconnect Audible in Settings.")
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

    with httpx.Client(cookies=cookies, headers=BASE_HEADERS, timeout=30.0, follow_redirects=True) as client:
        headers_field = _auth_headers_field(_fetch_config(client))

        cursor = ""
        while pages < MAX_PAGES:
            payload = _fetch_page(client, amc.api_base(), headers_field, cursor)
            tracks = amc.parse_tracks(payload)
            pages += 1

            if tracks:
                new, updated = upsert_tracks(db, tracks, started)
                new_total += new
                updated_total += updated
                seen.update(t.track_asin for t in tracks)

            cursor = amc.parse_next_cursor(payload)
            if not cursor:
                break
            time.sleep(PAGE_DELAY_SECONDS)

    missing = flag_missing(db, seen, started)
    mark_compilations(db)

    return {
        "pages": pages,
        "tracks_seen": len(seen),
        "new": new_total,
        "updated": updated_total,
        "missing": missing,
        "finished_at": datetime.utcnow(),
    }
