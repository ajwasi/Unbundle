import json
from pathlib import Path
from unittest.mock import patch

from app.connectors import amazon_music_connector as amc
from app.connectors.amazon_music_connector import AmazonMusicAuthError
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
    sync.flag_missing(db, {"B076HFF4Q3"})

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
