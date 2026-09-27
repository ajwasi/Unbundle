import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.amazon_music import downloader
from app.connectors import amazon_music_connector as amc
from app.connectors.amazon_music_connector import AmazonMusicAuthError
from app.models.amazon_music_destination import AmazonMusicDestination
from app.models.amazon_music_track import AmazonMusicTrack
from app.sync import amazon_music_sync as sync

FIXTURE = Path(__file__).parent / "fixtures" / "amazon_music_purchased_tracks.json"


def _seed(db):
    sync.upsert_tracks(db, amc.parse_tracks(json.loads(FIXTURE.read_text(encoding="utf-8"))))


def test_page_requires_auth(client):
    assert client.get("/amazon-music", follow_redirects=False).status_code == 303


def test_empty_state_points_at_the_sync_button(authed_client):
    resp = authed_client.get("/amazon-music")
    assert resp.status_code == 200
    assert "Nothing synced yet" in resp.text


def test_page_lists_synced_tracks(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music")
    assert "Every Breath You Take" in resp.text
    assert "The Police" in resp.text
    assert "Synchronicity" in resp.text
    assert "4:13" in resp.text


def test_search_matches_title_artist_and_album(authed_client, db):
    _seed(db)
    assert "Every Breath" in authed_client.get("/amazon-music", params={"q": "police"}).text
    assert "Every Breath" in authed_client.get("/amazon-music", params={"q": "synchron"}).text
    assert "Every Breath" not in authed_client.get("/amazon-music", params={"q": "garth"}).text


def test_missing_tracks_are_hidden_until_asked_for(authed_client, db):
    _seed(db)
    sync.flag_missing(db, {"c64eeeb1-e203-4c0a-9213-43ac6202c74a"})  # "Every Breath You Take"'s download_id

    hidden = authed_client.get("/amazon-music")
    assert "The Dance" not in hidden.text
    assert "no longer listed" in hidden.text  # the toggle label

    shown = authed_client.get("/amazon-music", params={"show_missing": "true"})
    assert "The Dance" in shown.text


def test_htmx_request_returns_only_the_table(authed_client, db):
    _seed(db)
    resp = authed_client.get("/amazon-music", headers={"HX-Request": "true"})
    assert "<html" not in resp.text
    assert 'id="amazon-music-table"' in resp.text


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


def test_table_shows_a_downloading_badge_instead_of_the_button_for_active_tracks(authed_client, db):
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

    # The active track's own per-row Download button is gone (replaced by a
    # badge); the other, unrelated track's button is still there.
    assert "/amazon-music/downloads/c64eeeb1-e203-4c0a-9213-43ac6202c74a" not in resp.text
    assert "/amazon-music/downloads/a941e672-3d37-43df-9c99-3232473fdf27" in resp.text
