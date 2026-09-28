import json
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import asset_cache
from app.amazon_music import downloader
from app.connectors import amazon_music_connector as amc
from app.connectors.amazon_music_connector import AmazonMusicAuthError
from app.models.amazon_music_album_catalog_sync import AmazonMusicAlbumCatalogSync
from app.models.amazon_music_destination import AmazonMusicDestination
from app.models.amazon_music_track import AmazonMusicTrack
from app.routers import amazon_music as amazon_music_router
from app.sync import amazon_music_sync as sync

FIXTURE = Path(__file__).parent / "fixtures" / "amazon_music_purchased_tracks.json"


def _seed(db):
    sync.upsert_tracks(db, amc.parse_tracks(json.loads(FIXTURE.read_text(encoding="utf-8"))))


def _select_all_note_is_hidden(html: str) -> bool:
    """Parses out just the select-all note's own opening tag and checks for
    a standalone `hidden` attribute — deliberately not a literal substring
    match against the whole tag, which would break on any harmless
    attribute-order or whitespace change in the template even though the
    actual behavior (hidden or not) didn't change.
    """
    match = re.search(r'<span id="amazon-music-select-all-note"[^>]*>', html)
    assert match, "select-all note span not found in response"
    tag = match.group(0)
    return bool(re.search(r'(^|\s)hidden(\s|=|>|$)', tag))


def test_page_requires_auth(client):
    assert client.get("/amazon-music", follow_redirects=False).status_code == 303


def test_albums_rows_route_requires_auth(client):
    # Pinned explicitly rather than relying solely on the blanket
    # AuthMiddleware covering it implicitly — this route was added after
    # test_page_requires_auth, and a future refactor that narrows the
    # middleware's scope should fail a test here, not ship a hole silently.
    assert client.get("/amazon-music/albums/rows", follow_redirects=False).status_code == 303


def test_album_cover_route_requires_auth(client):
    assert client.get("/amazon-music/albums/B074JM9JHY/cover", follow_redirects=False).status_code == 303


def test_refresh_cover_route_requires_auth(client):
    assert client.post("/amazon-music/albums/B074JM9JHY/refresh-cover", follow_redirects=False).status_code == 303


def test_bulk_refresh_covers_route_requires_auth(client):
    assert client.post("/amazon-music/albums/refresh-covers", follow_redirects=False).status_code == 303


def test_empty_state_points_at_the_sync_button(authed_client):
    resp = authed_client.get("/amazon-music")
    assert resp.status_code == 200
    assert "Nothing synced yet" in resp.text


def test_page_lists_synced_albums(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music")
    assert "Synchronicity" in resp.text
    assert "The Police" in resp.text
    assert "No Fences" in resp.text
    assert "/amazon-music/albums/B074JM9JHY" in resp.text


def test_page_shows_album_cover_art(authed_client, db):
    _seed(db)  # "Every Breath You Take" carries a real cover_url in the fixture
    resp = authed_client.get("/amazon-music")
    assert "<img" in resp.text


def test_album_covers_are_served_through_the_local_cache_route(authed_client, db):
    # The point of the cache: templates never embed Amazon's own (presigned,
    # expiring) cover_url directly — every <img> points at this app's own
    # cover route instead.
    _seed(db)
    resp = authed_client.get("/amazon-music")
    assert "/amazon-music/albums/B074JM9JHY/cover" in resp.text
    assert "m.media-amazon.com" not in resp.text


def test_album_cover_route_fetches_and_serves_the_cached_file(authed_client, db, tmp_path):
    _seed(db)
    cached_file = tmp_path / "art.jpg"
    cached_file.write_bytes(b"CACHED IMAGE BYTES")

    with patch.object(asset_cache, "get_or_fetch", return_value=cached_file) as mock_fetch:
        resp = authed_client.get("/amazon-music/albums/B074JM9JHY/cover")

    assert resp.status_code == 200
    assert resp.content == b"CACHED IMAGE BYTES"
    mock_fetch.assert_called_once()
    assert mock_fetch.call_args.args[0] == "amazon-music-album"
    # Exact match, not a substring check — CodeQL flags "host in url" as an
    # incomplete/bypassable sanitization pattern even in a test assertion
    # with no security decision behind it; asserting the fixture's full,
    # known cover_url sidesteps that pattern and is a more precise test.
    assert mock_fetch.call_args.args[1] == (
        "https://m.media-amazon.com/images/I/example_256x256.jpg?X-Amz-Expires=3600&X-Amz-Signature=deadbeef"
    )


def test_album_cover_route_404s_when_nothing_is_cacheable(authed_client, db):
    _seed(db)
    with patch.object(asset_cache, "get_or_fetch", return_value=None):
        resp = authed_client.get("/amazon-music/albums/B074JM9JHY/cover")
    assert resp.status_code == 404


def test_album_cover_route_404s_for_an_unknown_album(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music/albums/NOTAREALASIN/cover")
    assert resp.status_code == 404


def test_album_cover_route_tells_the_browser_to_cache_it_forever(authed_client, db, tmp_path):
    _seed(db)
    cached_file = tmp_path / "art.jpg"
    cached_file.write_bytes(b"CACHED IMAGE BYTES")
    with patch.object(asset_cache, "get_or_fetch", return_value=cached_file):
        resp = authed_client.get("/amazon-music/albums/B074JM9JHY/cover")
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_album_cover_route_gives_the_download_a_friendly_filename(authed_client, db, tmp_path):
    _seed(db)
    cached_file = tmp_path / "somehash.jpg"
    cached_file.write_bytes(b"CACHED IMAGE BYTES")
    with patch.object(asset_cache, "get_or_fetch", return_value=cached_file):
        resp = authed_client.get("/amazon-music/albums/B074JM9JHY/cover")
    assert "Synchronicity" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.jpg"')


def test_cover_route_reuses_one_resolved_url_per_request_not_a_full_album_scan(authed_client, db, tmp_path):
    # Regression guard for the redundant-query fix: resolving which URL to
    # check the cache under must not re-run a full per-track scan of the
    # album on every single cover request.
    _seed(db)
    cached_file = tmp_path / "art.jpg"
    cached_file.write_bytes(b"CACHED IMAGE BYTES")
    with patch.object(asset_cache, "get_or_fetch", return_value=cached_file):
        with patch.object(amazon_music_router, "_album_summary") as mock_summary:
            authed_client.get("/amazon-music/albums/B074JM9JHY/cover")
    mock_summary.assert_not_called()


def test_refresh_cover_route_evicts_the_cached_file_and_returns_an_img_tag(authed_client, db):
    _seed(db)
    with patch.object(asset_cache, "evict") as mock_evict:
        resp = authed_client.post("/amazon-music/albums/B074JM9JHY/refresh-cover")

    assert resp.status_code == 200
    mock_evict.assert_called_once_with(
        "amazon-music-album",
        "https://m.media-amazon.com/images/I/example_256x256.jpg?X-Amz-Expires=3600&X-Amz-Signature=deadbeef",
        reason="user requested a refresh",
    )
    assert "<img" in resp.text
    assert "/amazon-music/albums/B074JM9JHY/cover?v=" in resp.text  # cache-busted


def test_refresh_cover_route_is_a_no_op_for_an_album_with_no_cover(authed_client, db):
    _add_two_track_album(db, album_asin="NOCOVER")
    with patch.object(asset_cache, "evict") as mock_evict:
        resp = authed_client.post("/amazon-music/albums/NOCOVER/refresh-cover")
    assert resp.status_code == 200
    mock_evict.assert_not_called()


def test_refresh_selected_covers_evicts_only_the_selected_albums(authed_client, db):
    _seed(db)
    with patch.object(asset_cache, "evict") as mock_evict:
        resp = authed_client.post("/amazon-music/albums/refresh-covers", data={"album_key": ["B074JM9JHY"]})

    assert resp.status_code == 200
    mock_evict.assert_called_once_with(
        "amazon-music-album",
        "https://m.media-amazon.com/images/I/example_256x256.jpg?X-Amz-Expires=3600&X-Amz-Signature=deadbeef",
        reason="bulk refresh requested",
    )


def test_refresh_selected_covers_with_no_selection_is_a_no_op(authed_client, db):
    # Deliberately not "refresh everything shown" like the download bulk
    # actions — forcing every visible cover to refetch is much heavier and
    # more surprising to make one accidental click away.
    _seed(db)
    with patch.object(asset_cache, "evict") as mock_evict:
        resp = authed_client.post("/amazon-music/albums/refresh-covers")
    assert resp.status_code == 200
    mock_evict.assert_not_called()


def test_refresh_selected_covers_skips_an_album_with_no_known_cover(authed_client, db):
    _add_two_track_album(db, album_asin="NOCOVER")
    with patch.object(asset_cache, "evict") as mock_evict:
        resp = authed_client.post("/amazon-music/albums/refresh-covers", data={"album_key": ["NOCOVER"]})
    assert resp.status_code == 200
    mock_evict.assert_not_called()


def test_album_detail_page_lists_its_own_tracks(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music/albums/B074JM9JHY")
    assert resp.status_code == 200
    assert "Every Breath You Take" in resp.text
    assert "The Police" in resp.text
    assert "4:13" in resp.text
    assert "The Dance" not in resp.text  # the other album's track stays out


def test_album_detail_page_shows_isrc_and_purchase_date_when_present(authed_client, db):
    _seed(db)
    from datetime import datetime as dt

    row = db.get(AmazonMusicTrack, "c64eeeb1-e203-4c0a-9213-43ac6202c74a")
    row.isrc = "GBAAM8300001"
    row.purchased_at = dt(2019, 8, 20)
    db.commit()

    resp = authed_client.get("/amazon-music/albums/B074JM9JHY")
    assert "GBAAM8300001" in resp.text
    assert "2019-08-20" in resp.text


def test_album_detail_page_omits_the_indicator_when_absent(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music/albums/B074JM9JHY")
    assert "ISRC" not in resp.text


def test_album_detail_page_shows_one_cover_and_no_per_row_covers(authed_client, db):
    _seed(db)  # "Every Breath You Take" carries a real cover_url in the fixture
    resp = authed_client.get("/amazon-music/albums/B074JM9JHY")

    assert resp.text.count("<img") == 1  # the single album-level cover, not one per track
    assert 'width="120"' in resp.text  # the larger, singular header image
    assert 'width="40"' not in resp.text  # the old per-row thumbnail size is gone


def test_album_detail_page_404s_for_an_unknown_key(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music/albums/NOTAREALASIN")
    assert resp.status_code == 404


def test_search_matches_album_or_artist(authed_client, db):
    _seed(db)
    assert "Synchronicity" in authed_client.get("/amazon-music", params={"q": "police"}).text
    assert "Synchronicity" in authed_client.get("/amazon-music", params={"q": "synchron"}).text
    assert "Synchronicity" not in authed_client.get("/amazon-music", params={"q": "garth"}).text


def test_album_detail_search_matches_title(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music/albums/B074JM9JHY", params={"q": "every breath"})
    assert "Every Breath You Take" in resp.text


def test_missing_only_album_is_hidden_until_asked_for(authed_client, db):
    _seed(db)
    sync.flag_missing(db, {"c64eeeb1-e203-4c0a-9213-43ac6202c74a"})  # marks "The Dance" (No Fences) missing

    hidden = authed_client.get("/amazon-music")
    assert "No Fences" not in hidden.text
    assert "Synchronicity" in hidden.text
    assert "no longer listed" in hidden.text  # the toggle label

    shown = authed_client.get("/amazon-music", params={"show_missing": "true"})
    assert "No Fences" in shown.text


def test_missing_tracks_within_an_album_are_hidden_until_asked_for(authed_client, db):
    _seed(db)
    sync.flag_missing(db, {"c64eeeb1-e203-4c0a-9213-43ac6202c74a"})  # marks "The Dance" missing

    hidden = authed_client.get("/amazon-music/albums/B076HBKJ6J")  # No Fences
    assert "The Dance" not in hidden.text
    assert "no longer listed" in hidden.text

    shown = authed_client.get("/amazon-music/albums/B076HBKJ6J", params={"show_missing": "true"})
    assert "The Dance" in shown.text


def test_htmx_request_returns_only_the_table(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music", headers={"HX-Request": "true"})
    assert "<html" not in resp.text
    assert 'id="amazon-music-table"' in resp.text


def test_album_detail_htmx_request_returns_only_the_table(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music/albums/B074JM9JHY", headers={"HX-Request": "true"})
    assert "<html" not in resp.text
    assert 'id="amazon-music-table"' in resp.text


# ------------------------------------------------------- on-demand track order

def test_album_detail_page_shows_not_yet_fetched_by_default(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music/albums/B074JM9JHY")
    assert "Track order not yet fetched" in resp.text
    assert "Sync track order" in resp.text
    assert "Re-sync" not in resp.text


def test_album_detail_page_shows_synced_status_when_present(authed_client, db):
    _seed(db)
    db.add(AmazonMusicAlbumCatalogSync(album_asin="B074JM9JHY", tracks_found=1, tracks_matched=1))
    db.commit()

    resp = authed_client.get("/amazon-music/albums/B074JM9JHY")
    assert "Track order synced" in resp.text
    assert "1 of 1 tracks matched" in resp.text
    assert "Re-sync track order" in resp.text


def test_no_album_bucket_never_shows_the_track_order_control(authed_client, db):
    _seed(db)
    resp = authed_client.get(f"/amazon-music/albums/{amazon_music_router._NO_ALBUM_KEY}")
    assert "Sync track order" not in resp.text


def test_sync_track_order_route_calls_the_sync_function(authed_client, db):
    _seed(db)
    with patch.object(sync, "sync_album_track_order", return_value={"tracks_found": 1, "tracks_matched": 1, "total_tracks": 1}) as mock_sync:
        resp = authed_client.post("/amazon-music/albums/B074JM9JHY/sync-track-order")

    assert resp.status_code == 200
    mock_sync.assert_called_once_with(db, "B074JM9JHY")


def test_sync_track_order_route_surfaces_amazons_error(authed_client, db):
    _seed(db)
    with patch.object(sync, "sync_album_track_order", side_effect=sync.AmazonMusicRequestError("Amazon said no")):
        resp = authed_client.post("/amazon-music/albums/B074JM9JHY/sync-track-order")

    assert resp.status_code == 200
    assert "Amazon said no" in resp.text


def test_sync_track_order_route_rejects_the_no_album_bucket(authed_client, db):
    _seed(db)
    with patch.object(sync, "sync_album_track_order") as mock_sync:
        resp = authed_client.post(f"/amazon-music/albums/{amazon_music_router._NO_ALBUM_KEY}/sync-track-order")

    assert resp.status_code == 200
    assert "a single Amazon album" in resp.text
    mock_sync.assert_not_called()


def test_table_shows_track_number_and_dash_for_unnumbered(authed_client, db):
    _seed(db)
    row = db.get(AmazonMusicTrack, "c64eeeb1-e203-4c0a-9213-43ac6202c74a")
    row.track_number = 3
    db.commit()
    now = datetime.utcnow()
    db.add(AmazonMusicTrack(
        download_id="unnumbered", track_asin="Z", album_asin="B074JM9JHY", album="Synchronicity",
        artist="The Police", title="Unnumbered Bonus Track", first_seen_at=now, last_seen_at=now,
    ))
    db.commit()

    resp = authed_client.get("/amazon-music/albums/B074JM9JHY")
    assert ">3<" in resp.text  # the numbered track
    assert ">—<" in resp.text  # the one with no track_number


def test_page_shows_ordered_badge_for_a_synced_album(authed_client, db):
    _seed(db)
    db.add(AmazonMusicAlbumCatalogSync(album_asin="B074JM9JHY", tracks_found=1, tracks_matched=1))
    db.commit()

    resp = authed_client.get("/amazon-music")
    assert "Ordered" in resp.text


def test_refresh_reports_a_successful_sync(authed_client, db):
    # No candidates_seen here on purpose: a summary that omits it (an older
    # caller, or a shape the router doesn't control) must render fine rather
    # than the template choking on a missing key.
    summary = {"pages": 2, "tracks_seen": 51, "new": 3, "updated": 48, "missing": 1}
    with patch.object(sync, "refresh_purchased_tracks", return_value=summary):
        resp = authed_client.post("/amazon-music/refresh")

    assert resp.status_code == 200
    assert "51 track(s) recognised" in resp.text
    assert "over 2 page(s)" in resp.text
    assert "3 new" in resp.text
    assert "out of" not in resp.text


def test_refresh_reports_tracks_found_only_by_the_second_pass(authed_client, db):
    summary = {"pages": 4, "tracks_seen": 51, "new": 3, "updated": 48, "missing": 1, "second_pass_only_tracks": 7}
    with patch.object(sync, "refresh_purchased_tracks", return_value=summary):
        resp = authed_client.post("/amazon-music/refresh")

    assert resp.status_code == 200
    assert "7" in resp.text
    assert "found only by" in resp.text


def test_refresh_with_no_second_pass_extras_omits_that_note(authed_client, db):
    summary = {"pages": 2, "tracks_seen": 51, "new": 3, "updated": 48, "missing": 1, "second_pass_only_tracks": 0}
    with patch.object(sync, "refresh_purchased_tracks", return_value=summary):
        resp = authed_client.post("/amazon-music/refresh")

    assert resp.status_code == 200
    assert "found only by" not in resp.text


def test_a_shortfall_with_no_captured_shapes_says_so_instead_of_vanishing(authed_client, db):
    # The real bug this guards: the warning and the disclosure used to be
    # gated on two different conditions (tracks_seen < candidates_seen vs.
    # unmatched_item_shapes being truthy), so a real sync could show "details
    # below are key names only" and then render nothing below it at all —
    # exactly what a live sync (27 recognised of 10,000 seen, 234 updated)
    # actually hit, since almost all of the shortfall was rows resolving to
    # an already-seen track rather than rows failing to match at all.
    summary = {"pages": 200, "tracks_seen": 27, "candidates_seen": 10000, "new": 0, "updated": 234, "missing": 0}
    with patch.object(sync, "refresh_purchased_tracks", return_value=summary):
        resp = authed_client.post("/amazon-music/refresh")

    assert resp.status_code == 200
    assert "Most rows on the page were not recognised" in resp.text
    assert "Why no unmatched row shape was captured" in resp.text
    assert "resolved to the same" in resp.text


def test_a_shortfall_with_captured_shapes_still_shows_them(authed_client, db):
    summary = {
        "pages": 1,
        "tracks_seen": 1,
        "candidates_seen": 3,
        "new": 1,
        "updated": 0,
        "missing": 0,
        "unmatched_item_shapes": [["componentType", "primaryText"]],
    }
    with patch.object(sync, "refresh_purchased_tracks", return_value=summary):
        resp = authed_client.post("/amazon-music/refresh")

    assert "Show rows that didn't look like a track (1)" in resp.text
    assert "Why no unmatched row shape was captured" not in resp.text


def test_refresh_without_audible_explains_rather_than_500s(authed_client):
    resp = authed_client.post("/amazon-music/refresh")
    assert resp.status_code == 200
    # Autoescaping turns the apostrophe in "isn't" into &#39;, so match a
    # stretch of the message that has none.
    assert "no Amazon login to sync music with" in resp.text


def test_expired_credentials_give_a_reconnect_message_not_a_500(authed_client):
    with patch.object(sync, "refresh_purchased_tracks", side_effect=AmazonMusicAuthError("Amazon rejected the stored login. Reconnect Audible in Settings.")):
        resp = authed_client.post("/amazon-music/refresh")

    assert resp.status_code == 200
    assert "Reconnect Audible" in resp.text


def test_an_unexpected_shape_change_reads_as_a_broken_connector(authed_client):
    with patch.object(sync, "refresh_purchased_tracks", side_effect=KeyError("widgets")):
        resp = authed_client.post("/amazon-music/refresh")

    assert resp.status_code == 200
    assert "Amazon may have changed this API" in resp.text


def test_refresh_is_rate_limited(authed_client):
    from app.routers.amazon_music import _refresh_limiter

    _refresh_limiter.reset()
    with patch.object(sync, "refresh_purchased_tracks", return_value={"pages": 1, "tracks_seen": 0, "new": 0, "updated": 0, "missing": 0}):
        for _ in range(5):
            assert authed_client.post("/amazon-music/refresh").status_code == 200
        assert authed_client.post("/amazon-music/refresh").status_code == 429


def test_sidebar_links_to_the_page(authed_client):
    assert 'href="/amazon-music"' in authed_client.get("/downloads").text


# ------------------------------------------------------------ destinations

def test_create_destination_adds_a_row_and_becomes_default_if_first(authed_client, db):
    resp = authed_client.post("/amazon-music/destinations", data={"name": "NAS", "path": "/mnt/music"})
    assert resp.status_code == 200
    assert "NAS" in resp.text
    assert "/mnt/music" in resp.text

    row = db.query(AmazonMusicDestination).one()
    assert row.is_default is True


def test_a_second_destination_does_not_become_default_automatically(authed_client, db):
    db.add(AmazonMusicDestination(name="First", path="/mnt/first", is_default=True))
    db.commit()

    authed_client.post("/amazon-music/destinations", data={"name": "Second", "path": "/mnt/second"})

    second = db.query(AmazonMusicDestination).filter(AmazonMusicDestination.name == "Second").one()
    assert second.is_default is False


def test_blank_name_or_path_is_ignored(authed_client, db):
    authed_client.post("/amazon-music/destinations", data={"name": "", "path": "/mnt/music"})
    authed_client.post("/amazon-music/destinations", data={"name": "NAS", "path": ""})
    assert db.query(AmazonMusicDestination).count() == 0


def test_set_default_destination_switches_the_flag(authed_client, db):
    a = AmazonMusicDestination(name="A", path="/mnt/a", is_default=True)
    b = AmazonMusicDestination(name="B", path="/mnt/b", is_default=False)
    db.add_all([a, b])
    db.commit()

    resp = authed_client.post(f"/amazon-music/destinations/{b.id}/set-default")
    assert resp.status_code == 200

    db.refresh(a)
    db.refresh(b)
    assert a.is_default is False
    assert b.is_default is True


def test_delete_destination_removes_it(authed_client, db):
    dest = AmazonMusicDestination(name="A", path="/mnt/a")
    db.add(dest)
    db.commit()
    dest_id = dest.id

    resp = authed_client.post(f"/amazon-music/destinations/{dest_id}/delete")
    assert resp.status_code == 200
    assert db.get(AmazonMusicDestination, dest_id) is None


# --------------------------------------------------------------- downloads

def test_download_track_queues_via_the_downloader(authed_client, db):
    _seed(db)
    with patch.object(downloader, "start_download", new=AsyncMock()) as mock_start:
        resp = authed_client.post("/amazon-music/downloads/c64eeeb1-e203-4c0a-9213-43ac6202c74a")

    assert resp.status_code == 200
    mock_start.assert_awaited_once()
    assert mock_start.call_args.args[1] == "c64eeeb1-e203-4c0a-9213-43ac6202c74a"


def test_download_track_ignores_an_unknown_id_instead_of_500ing(authed_client, db):
    _seed(db)
    with patch.object(downloader, "start_download", new=AsyncMock(side_effect=downloader.UnknownTrackError("x"))):
        resp = authed_client.post("/amazon-music/downloads/not-a-real-id")
    assert resp.status_code == 200


def test_downloads_with_a_selection_queues_only_those(authed_client, db):
    _seed(db)
    with patch.object(downloader, "queue_many", new=AsyncMock()) as mock_queue:
        resp = authed_client.post(
            "/amazon-music/downloads", data={"download_id": ["c64eeeb1-e203-4c0a-9213-43ac6202c74a"]}
        )

    assert resp.status_code == 200
    mock_queue.assert_awaited_once()
    assert mock_queue.call_args.args[1] == ["c64eeeb1-e203-4c0a-9213-43ac6202c74a"]


def test_downloads_with_no_selection_queues_everything_currently_filtered(authed_client, db):
    _seed(db)
    with patch.object(downloader, "queue_many", new=AsyncMock()) as mock_queue:
        resp = authed_client.post("/amazon-music/downloads", data={"q": "police"})

    assert resp.status_code == 200
    mock_queue.assert_awaited_once()
    # Only the row matching the search, not the whole library.
    assert mock_queue.call_args.args[1] == ["c64eeeb1-e203-4c0a-9213-43ac6202c74a"]


def test_active_downloads_endpoint_renders_the_polling_partial(authed_client, db):
    _seed(db)
    fake_active = [
        {
            "id": 1,
            "download_id": "c64eeeb1-e203-4c0a-9213-43ac6202c74a",
            "title": "Every Breath You Take",
            "artist": "The Police",
            "status": "running",
            "progress_bytes": 100,
            "expected_size_bytes": 200,
        }
    ]
    with patch.object(downloader, "get_active_downloads", return_value=fake_active):
        resp = authed_client.get("/amazon-music/downloads/active")

    assert resp.status_code == 200
    assert "Every Breath You Take" in resp.text
    assert "Downloading" in resp.text


def test_album_page_shows_a_downloading_badge_instead_of_the_button_for_active_tracks(authed_client, db):
    _seed(db)
    fake_active = [
        {
            "id": 1,
            "download_id": "c64eeeb1-e203-4c0a-9213-43ac6202c74a",
            "title": "Every Breath You Take",
            "artist": "The Police",
            "status": "queued",
            "progress_bytes": 0,
            "expected_size_bytes": None,
        }
    ]
    with patch.object(downloader, "get_active_downloads", return_value=fake_active):
        resp = authed_client.get("/amazon-music/albums/B074JM9JHY")

    assert "/amazon-music/downloads/c64eeeb1-e203-4c0a-9213-43ac6202c74a" not in resp.text
    assert "Downloading" in resp.text


def test_main_page_shows_a_downloading_badge_for_an_album_with_an_active_track(authed_client, db):
    _seed(db)
    fake_active = [
        {
            "id": 1,
            "download_id": "c64eeeb1-e203-4c0a-9213-43ac6202c74a",
            "title": "Every Breath You Take",
            "artist": "The Police",
            "status": "queued",
            "progress_bytes": 0,
            "expected_size_bytes": None,
        }
    ]
    with patch.object(downloader, "get_active_downloads", return_value=fake_active):
        resp = authed_client.get("/amazon-music")

    # The active track's album has no per-album Download button anymore
    # (replaced by a badge); the other, unrelated album's button is still there.
    assert "/amazon-music/albums/B074JM9JHY/download" not in resp.text
    assert "/amazon-music/albums/B076HBKJ6J/download" in resp.text


# ---------------------------------------------------------- album downloads

def test_download_album_queues_all_its_tracks(authed_client, db):
    _seed(db)
    with patch.object(downloader, "queue_many", new=AsyncMock()) as mock_queue:
        resp = authed_client.post("/amazon-music/albums/B074JM9JHY/download")

    assert resp.status_code == 200
    mock_queue.assert_awaited_once()
    assert mock_queue.call_args.args[1] == ["c64eeeb1-e203-4c0a-9213-43ac6202c74a"]


def test_album_downloads_with_a_selection_queues_only_those_albums(authed_client, db):
    _seed(db)
    with patch.object(downloader, "queue_many", new=AsyncMock()) as mock_queue:
        resp = authed_client.post("/amazon-music/albums/downloads", data={"album_key": ["B074JM9JHY"]})

    assert resp.status_code == 200
    mock_queue.assert_awaited_once()
    assert mock_queue.call_args.args[1] == ["c64eeeb1-e203-4c0a-9213-43ac6202c74a"]


def test_album_downloads_with_no_selection_queues_every_track(authed_client, db):
    _seed(db)
    with patch.object(downloader, "queue_many", new=AsyncMock()) as mock_queue:
        resp = authed_client.post("/amazon-music/albums/downloads")

    assert resp.status_code == 200
    mock_queue.assert_awaited_once()
    queued = set(mock_queue.call_args.args[1])
    assert queued == {"c64eeeb1-e203-4c0a-9213-43ac6202c74a", "a941e672-3d37-43df-9c99-3232473fdf27"}


def test_album_downloads_search_scopes_the_no_selection_case(authed_client, db):
    _seed(db)
    with patch.object(downloader, "queue_many", new=AsyncMock()) as mock_queue:
        resp = authed_client.post("/amazon-music/albums/downloads", data={"q": "police"})

    assert resp.status_code == 200
    mock_queue.assert_awaited_once()
    assert mock_queue.call_args.args[1] == ["c64eeeb1-e203-4c0a-9213-43ac6202c74a"]


# ------------------------------------------------------------- album rows

def _add_two_track_album(db, album_asin="ALBUM1", **overrides):
    now = datetime.utcnow()
    defaults = {"is_compilation": False, "cover_url": ""}
    first = {"download_id": "d1", "track_asin": "A1", "album_asin": album_asin, "album": "Greatest Hits",
             "artist": "Artist One", "title": "Song A", "first_seen_at": now, "last_seen_at": now}
    second = {"download_id": "d2", "track_asin": "A2", "album_asin": album_asin, "album": "Greatest Hits",
              "artist": "Artist One", "title": "Song B", "first_seen_at": now, "last_seen_at": now}
    first.update(defaults)
    second.update(defaults)
    first.update(overrides.get("first", {}))
    second.update(overrides.get("second", {}))
    db.add(AmazonMusicTrack(**first))
    db.add(AmazonMusicTrack(**second))
    db.commit()


def test_album_rows_groups_by_album_asin_and_counts_tracks(db):
    _add_two_track_album(db, first={"cover_url": "https://example.invalid/a.jpg"})

    rows = amazon_music_router._album_rows(db)

    assert len(rows) == 1
    album = rows[0]
    assert album["album_key"] == "ALBUM1"
    assert album["track_count"] == 2
    assert album["cover_url"] == "https://example.invalid/a.jpg"
    assert album["artist"] == "Artist One"


def test_album_rows_buckets_tracks_with_no_album_asin_together(db):
    _add_two_track_album(db, album_asin="")

    rows = amazon_music_router._album_rows(db)

    assert len(rows) == 1
    assert rows[0]["album_key"] == amazon_music_router._NO_ALBUM_KEY
    assert rows[0]["track_count"] == 2


def test_album_rows_shows_various_artists_for_a_compilation(db):
    _add_two_track_album(
        db,
        first={"artist": "Artist One", "is_compilation": True},
        second={"artist": "Artist Two", "is_compilation": True},
    )

    rows = amazon_music_router._album_rows(db)

    assert rows[0]["artist"] == "Various Artists"


def test_album_rows_counts_missing_tracks_without_hiding_the_album(db):
    _add_two_track_album(db)
    db.get(AmazonMusicTrack, "d2").missing_since = datetime.utcnow()
    db.commit()

    rows = amazon_music_router._album_rows(db, show_missing=True)

    assert rows[0]["track_count"] == 2
    assert rows[0]["missing_count"] == 1


def test_album_rows_prefers_the_most_recently_refreshed_cover(db):
    # The real bug this guards: cover_url is a presigned, expiring URL.
    # Picking merely the first non-blank one seen (in arbitrary query order)
    # can land on one that expired sync-runs ago, even while a later-synced
    # track in the same album has a live one. "First" here is track d1
    # (added first in _add_two_track_album), but its cover is the *older*
    # one — the album must still pick d2's fresher cover.
    stale = datetime(2020, 1, 1)
    fresh = datetime(2026, 1, 1)
    _add_two_track_album(
        db,
        first={"cover_url": "https://example.invalid/stale.jpg", "cover_url_refreshed_at": stale},
        second={"cover_url": "https://example.invalid/fresh.jpg", "cover_url_refreshed_at": fresh},
    )

    rows = amazon_music_router._album_rows(db)

    assert rows[0]["cover_url"] == "https://example.invalid/fresh.jpg"


def test_album_rows_keeps_the_fresher_cover_even_when_seen_first(db):
    # Same as above with the fresh one on the *first*-processed track, to
    # confirm this is genuinely freshness-based and not just "last one wins".
    stale = datetime(2020, 1, 1)
    fresh = datetime(2026, 1, 1)
    _add_two_track_album(
        db,
        first={"cover_url": "https://example.invalid/fresh.jpg", "cover_url_refreshed_at": fresh},
        second={"cover_url": "https://example.invalid/stale.jpg", "cover_url_refreshed_at": stale},
    )

    rows = amazon_music_router._album_rows(db)

    assert rows[0]["cover_url"] == "https://example.invalid/fresh.jpg"


def test_album_summary_prefers_the_most_recently_refreshed_cover(db):
    stale = datetime(2020, 1, 1)
    fresh = datetime(2026, 1, 1)
    _add_two_track_album(
        db,
        first={"cover_url": "https://example.invalid/stale.jpg", "cover_url_refreshed_at": stale},
        second={"cover_url": "https://example.invalid/fresh.jpg", "cover_url_refreshed_at": fresh},
    )

    summary = amazon_music_router._album_summary(db, "ALBUM1")

    assert summary["cover_url"] == "https://example.invalid/fresh.jpg"


def test_sort_albums_by_track_count_descending():
    albums = [
        {"album": "A", "album_key": "A1", "artist": "X", "track_count": 1, "first_seen_at": datetime(2020, 1, 1)},
        {"album": "B", "album_key": "B1", "artist": "Y", "track_count": 5, "first_seen_at": datetime(2020, 1, 1)},
    ]
    assert [a["album"] for a in amazon_music_router._sort_albums(albums, "tracks")] == ["B", "A"]


def test_sort_albums_by_name():
    albums = [
        {"album": "Zebra", "album_key": "Z1", "artist": "X", "track_count": 1, "first_seen_at": datetime(2020, 1, 1)},
        {"album": "Apple", "album_key": "A1", "artist": "Y", "track_count": 1, "first_seen_at": datetime(2020, 1, 1)},
    ]
    assert [a["album"] for a in amazon_music_router._sort_albums(albums, "album")] == ["Apple", "Zebra"]


def test_sort_albums_breaks_ties_deterministically_by_album_key():
    # Same first_seen_at for both — plausible in practice since a whole sync
    # run can stamp every track with the same timestamp. Without a secondary
    # sort key, which one comes first would depend on incidental dict/query
    # order instead of being deterministic.
    same_time = datetime(2020, 1, 1)
    albums = [
        {"album": "B", "album_key": "B1", "artist": "X", "track_count": 1, "first_seen_at": same_time},
        {"album": "A", "album_key": "A1", "artist": "Y", "track_count": 1, "first_seen_at": same_time},
    ]
    result = amazon_music_router._sort_albums(albums, "added")
    assert [a["album_key"] for a in result] == ["B1", "A1"]  # reverse=True sorts the tiebreaker too


# ------------------------------------------------------------ _safe_filename

def test_safe_filename_passes_through_a_plain_name():
    assert amazon_music_router._safe_filename("Synchronicity", ".jpg") == "Synchronicity.jpg"


def test_safe_filename_replaces_a_literal_slash():
    # A real, common case — plenty of albums are legitimately named this way.
    assert amazon_music_router._safe_filename("AC/DC", ".jpg") == "AC_DC.jpg"


def test_safe_filename_replaces_every_unsafe_character():
    result = amazon_music_router._safe_filename('a\\b/c:d*e?f"g<h>i|j', ".jpg")
    assert result == "a_b_c_d_e_f_g_h_i_j.jpg"


def test_safe_filename_of_all_unsafe_characters_replaces_rather_than_empties():
    # Unsafe characters map to "_", which strip(" .") never removes — only a
    # name that's already nothing but spaces/dots collapses to the "cover"
    # fallback (see test_safe_filename_falls_back_to_cover_for_dots_only).
    assert amazon_music_router._safe_filename("///", ".jpg") == "___.jpg"


def test_safe_filename_falls_back_to_cover_for_a_blank_name():
    assert amazon_music_router._safe_filename("", ".jpg") == "cover.jpg"


def test_safe_filename_falls_back_to_cover_for_dots_only():
    assert amazon_music_router._safe_filename("...", ".jpg") == "cover.jpg"


def test_safe_filename_truncates_an_overlong_name():
    result = amazon_music_router._safe_filename("x" * 200, ".jpg")
    assert result == ("x" * 80) + ".jpg"


def test_safe_filename_strips_cr_and_lf():
    # Header-injection-shaped input — a newline in a filename could otherwise
    # smuggle extra header lines into the Content-Disposition response.
    result = amazon_music_router._safe_filename("evil\r\nX-Injected: yes", ".jpg")
    assert "\r" not in result and "\n" not in result


# --------------------------------------------------------- albums pagination

def _add_n_albums(db, n):
    now = datetime.utcnow()
    for i in range(n):
        db.add(
            AmazonMusicTrack(
                download_id=f"pg-d{i:05d}",
                track_asin=f"PG{i:08d}",
                album_asin=f"ALBUM{i:04d}",
                album=f"Album {i:04d}",
                artist="Artist One",
                title=f"Song {i:04d}",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    db.commit()


def test_albums_context_slices_to_one_page(db):
    _add_n_albums(db, amazon_music_router._ALBUMS_PAGE_SIZE + 20)

    context = amazon_music_router._albums_context(db)

    assert len(context["albums"]) == amazon_music_router._ALBUMS_PAGE_SIZE
    assert context["has_more"] is True
    assert context["next_offset"] == amazon_music_router._ALBUMS_PAGE_SIZE
    assert context["total_albums"] == amazon_music_router._ALBUMS_PAGE_SIZE + 20
    assert context["shown_so_far"] == amazon_music_router._ALBUMS_PAGE_SIZE


def test_albums_context_last_page_has_no_more(db):
    _add_n_albums(db, amazon_music_router._ALBUMS_PAGE_SIZE + 20)

    context = amazon_music_router._albums_context(db, offset=amazon_music_router._ALBUMS_PAGE_SIZE)

    assert len(context["albums"]) == 20
    assert context["has_more"] is False
    assert context["shown_so_far"] == amazon_music_router._ALBUMS_PAGE_SIZE + 20


def test_albums_context_under_a_page_reports_no_more(db):
    _add_two_track_album(db)

    context = amazon_music_router._albums_context(db)

    assert len(context["albums"]) == 1
    assert context["has_more"] is False
    assert context["total_albums"] == 1


def test_albums_page_renders_a_sentinel_row_when_more_remain(authed_client, db):
    _add_n_albums(db, amazon_music_router._ALBUMS_PAGE_SIZE + 1)

    resp = authed_client.get("/amazon-music")

    assert resp.status_code == 200
    assert 'id="amazon-music-albums-sentinel"' in resp.text
    assert resp.text.count('class="amazon-music-album-row"') == amazon_music_router._ALBUMS_PAGE_SIZE


def test_albums_page_omits_the_sentinel_when_everything_fits(authed_client, db):
    _add_two_track_album(db)

    resp = authed_client.get("/amazon-music")

    assert 'id="amazon-music-albums-sentinel"' not in resp.text


def test_albums_rows_route_serves_the_next_batch(authed_client, db):
    _add_n_albums(db, amazon_music_router._ALBUMS_PAGE_SIZE + 20)

    resp = authed_client.get(
        "/amazon-music/albums/rows", params={"offset": amazon_music_router._ALBUMS_PAGE_SIZE}
    )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert resp.text.count('class="amazon-music-album-row"') == 20
    assert 'id="amazon-music-albums-sentinel"' not in resp.text


def test_albums_rows_route_respects_search_and_sort(authed_client, db):
    _add_two_track_album(
        db,
        album_asin="ALBUM_A",
        first={"download_id": "za1", "track_asin": "ZA1", "artist": "Zzz Band"},
        second={"download_id": "za2", "track_asin": "ZA2", "artist": "Zzz Band"},
    )
    _add_two_track_album(
        db, album_asin="ALBUM_B", first={"download_id": "zb1", "track_asin": "ZB1"}, second={"download_id": "zb2", "track_asin": "ZB2"}
    )

    resp = authed_client.get("/amazon-music/albums/rows", params={"q": "zzz"})

    assert "Zzz Band" in resp.text
    assert "Artist One" not in resp.text


def test_albums_rows_route_rejects_a_negative_offset(authed_client, db):
    resp = authed_client.get("/amazon-music/albums/rows", params={"offset": -5})
    assert resp.status_code == 422


def test_albums_context_clamps_a_negative_offset_defensively(db):
    # Deliberately more than _ALBUMS_PAGE_SIZE (100) albums: with a small
    # list, Python's own negative-index slicing "accidentally" self-corrects
    # (e.g. a 1-item list sliced [-50:50] just gives back the 1 item anyway),
    # so a test seeding too few albums would pass identically whether or not
    # the offset = max(0, offset) clamp actually exists — this needs a list
    # long enough for [-50:50] to produce a genuinely different, wrong,
    # empty-or-truncated slice without the clamp.
    _add_n_albums(db, amazon_music_router._ALBUMS_PAGE_SIZE * 2)
    context = amazon_music_router._albums_context(db, offset=-50)
    assert len(context["albums"]) == amazon_music_router._ALBUMS_PAGE_SIZE
    assert context["shown_so_far"] == amazon_music_router._ALBUMS_PAGE_SIZE


# ---------------------------------------------------- single-row re-rendering

def test_single_album_download_returns_only_that_row_not_the_whole_grid(authed_client, db):
    _seed(db)
    with patch.object(downloader, "queue_many", new=AsyncMock()):
        resp = authed_client.post("/amazon-music/albums/B074JM9JHY/download")

    assert resp.status_code == 200
    assert 'id="amazon-music-table"' not in resp.text
    assert "Synchronicity" in resp.text
    assert "No Fences" not in resp.text  # the other album never appears in a single-row response


def test_single_album_download_row_shows_the_downloading_badge(authed_client, db):
    _seed(db)
    with patch.object(downloader, "queue_many", new=AsyncMock()):
        with patch.object(downloader, "get_active_downloads", return_value=[{"download_id": "c64eeeb1-e203-4c0a-9213-43ac6202c74a"}]):
            resp = authed_client.post("/amazon-music/albums/B074JM9JHY/download")

    assert "Downloading" in resp.text


def test_single_album_download_of_a_stale_album_removes_the_row(authed_client, db):
    _seed(db)
    with patch.object(downloader, "queue_many", new=AsyncMock()):
        resp = authed_client.post("/amazon-music/albums/NOTAREALASIN/download")
    assert resp.status_code == 200
    assert "<tr" not in resp.text


# ------------------------------------------------------------ cache warm-up

def test_a_successful_refresh_schedules_a_cover_warmup(authed_client, db):
    summary = {"pages": 1, "tracks_seen": 1, "new": 1, "updated": 0, "missing": 0}
    with patch.object(sync, "refresh_purchased_tracks", return_value=summary):
        with patch.object(amazon_music_router, "_warm_album_covers") as mock_warm:
            authed_client.post("/amazon-music/refresh")
    mock_warm.assert_called_once()


def test_a_failed_refresh_does_not_schedule_a_warmup(authed_client, db):
    with patch.object(sync, "refresh_purchased_tracks", side_effect=sync.NotConnectedError("not connected")):
        with patch.object(amazon_music_router, "_warm_album_covers") as mock_warm:
            authed_client.post("/amazon-music/refresh")
    mock_warm.assert_not_called()


def test_warm_album_covers_fetches_every_known_cover(db):
    _seed(db)
    with patch.object(asset_cache, "get_or_fetch") as mock_fetch:
        amazon_music_router._warm_album_covers()
    urls = {call.args[1] for call in mock_fetch.call_args_list}
    assert "https://m.media-amazon.com/images/I/example_256x256.jpg?X-Amz-Expires=3600&X-Amz-Signature=deadbeef" in urls


def test_warm_album_covers_does_not_raise_when_a_fetch_fails(db):
    _seed(db)
    with patch.object(asset_cache, "get_or_fetch", side_effect=RuntimeError("boom")):
        amazon_music_router._warm_album_covers()  # must not propagate


def test_warm_album_covers_continues_past_one_failing_album(db):
    # Regression guard: the try/except must sit inside the per-album loop,
    # not wrap the whole thing — otherwise one bad album aborts warm-up for
    # every album after it in the same run.
    _add_two_track_album(db, album_asin="ALBUM_FAILS", first={"cover_url": "https://example.invalid/a.jpg"})
    _add_two_track_album(
        db,
        album_asin="ALBUM_OK",
        first={"download_id": "ok1", "track_asin": "OK1", "cover_url": "https://example.invalid/b.jpg"},
        second={"download_id": "ok2", "track_asin": "OK2"},
    )
    calls = []

    def fetch(namespace, url):
        calls.append(url)
        if url.endswith("a.jpg"):
            raise RuntimeError("boom")
        return None

    with patch.object(asset_cache, "get_or_fetch", side_effect=fetch):
        with patch.object(amazon_music_router.time, "sleep"):
            amazon_music_router._warm_album_covers()

    assert "https://example.invalid/a.jpg" in calls
    assert "https://example.invalid/b.jpg" in calls  # reached despite the first album raising


def test_warm_album_covers_paces_only_real_fetches(db):
    _add_two_track_album(db, first={"cover_url": "https://example.invalid/a.jpg"})
    with patch.object(asset_cache, "get_or_fetch", return_value=None):
        with patch.object(amazon_music_router.time, "sleep") as mock_sleep:
            amazon_music_router._warm_album_covers()
    mock_sleep.assert_called_once_with(amazon_music_router._WARM_UP_PACING_SECONDS)


def test_warm_album_covers_skips_pacing_when_already_cached(db, tmp_path):
    _add_two_track_album(db, first={"cover_url": "https://example.invalid/a.jpg"})
    with patch.object(asset_cache, "cached_path", return_value=tmp_path / "already-there.jpg"):
        with patch.object(asset_cache, "get_or_fetch", return_value=tmp_path / "already-there.jpg"):
            with patch.object(amazon_music_router.time, "sleep") as mock_sleep:
                amazon_music_router._warm_album_covers()
    mock_sleep.assert_not_called()


def test_warm_album_covers_skips_a_fully_missing_album(db):
    now = datetime.utcnow()
    _add_two_track_album(
        db,
        album_asin="ALBUM_GONE",
        first={"cover_url": "https://example.invalid/gone.jpg", "missing_since": now},
        second={"missing_since": now},
    )
    with patch.object(asset_cache, "get_or_fetch") as mock_fetch:
        with patch.object(amazon_music_router.time, "sleep"):
            amazon_music_router._warm_album_covers()
    urls = {call.args[1] for call in mock_fetch.call_args_list}
    assert "https://example.invalid/gone.jpg" not in urls


def test_warm_album_covers_does_not_run_two_passes_concurrently(db):
    _add_two_track_album(db, first={"cover_url": "https://example.invalid/a.jpg"})
    assert amazon_music_router._warm_up_lock.acquire(blocking=False)
    try:
        with patch.object(asset_cache, "get_or_fetch") as mock_fetch:
            amazon_music_router._warm_album_covers()  # lock already held — should no-op
        mock_fetch.assert_not_called()
    finally:
        amazon_music_router._warm_up_lock.release()


# --------------------------------------------------------- cover info cache

def test_resolve_cover_info_invalidates_when_a_cover_is_refreshed(db):
    stale = datetime(2020, 1, 1)
    _add_two_track_album(db, first={"cover_url": "https://example.invalid/stale.jpg", "cover_url_refreshed_at": stale})
    first = amazon_music_router._resolve_cover_info(db)
    assert first["ALBUM1"]["url"] == "https://example.invalid/stale.jpg"

    fresh = datetime(2026, 1, 1)
    row = db.query(AmazonMusicTrack).filter_by(download_id="d1").one()
    row.cover_url = "https://example.invalid/fresh.jpg"
    row.cover_url_refreshed_at = fresh
    db.commit()

    second = amazon_music_router._resolve_cover_info(db)
    assert second["ALBUM1"]["url"] == "https://example.invalid/fresh.jpg"


# ------------------------------------------------------- select-all scope note

def test_select_all_note_hidden_when_everything_is_loaded(authed_client, db):
    _add_two_track_album(db)
    resp = authed_client.get("/amazon-music")
    assert _select_all_note_is_hidden(resp.text) is True


def test_select_all_note_shown_when_more_albums_remain(authed_client, db):
    _add_n_albums(db, amazon_music_router._ALBUMS_PAGE_SIZE + 1)
    resp = authed_client.get("/amazon-music")
    assert _select_all_note_is_hidden(resp.text) is False


def test_select_all_note_is_announced_to_assistive_tech(authed_client, db):
    _add_two_track_album(db)
    resp = authed_client.get("/amazon-music")
    match = re.search(r'<span id="amazon-music-select-all-note"[^>]*>', resp.text)
    assert match and 'aria-live="polite"' in match.group(0)
