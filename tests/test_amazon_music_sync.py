import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.connectors import amazon_music_connector as amc
from app.models.amazon_music_track import AmazonMusicTrack
from app.sync import amazon_music_sync as sync

FIXTURE = Path(__file__).parent / "fixtures" / "amazon_music_purchased_tracks.json"


def _tracks():
    return amc.parse_tracks(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_upsert_inserts_then_updates(db):
    new, updated = sync.upsert_tracks(db, _tracks())
    assert (new, updated) == (2, 0)

    new, updated = sync.upsert_tracks(db, _tracks())
    assert (new, updated) == (0, 2)
    assert db.query(AmazonMusicTrack).count() == 2


def test_upsert_stores_the_mapped_fields(db):
    sync.upsert_tracks(db, _tracks())
    row = db.get(AmazonMusicTrack, "B076HFF4Q3")

    assert row.title == "Every Breath You Take"
    assert row.album_asin == "B074JM9JHY"
    assert row.download_id == "c64eeeb1-e203-4c0a-9213-43ac6202c74a"
    assert row.duration_seconds == 253
    assert row.cover_url_refreshed_at is not None


def test_a_blank_cover_never_overwrites_a_good_one(db):
    sync.upsert_tracks(db, _tracks())
    row = db.get(AmazonMusicTrack, "B07BFJR1HC")
    assert row.cover_url == ""  # fixture row has no image

    row.cover_url = "https://example.invalid/art.jpg"
    db.commit()
    sync.upsert_tracks(db, _tracks())
    assert db.get(AmazonMusicTrack, "B07BFJR1HC").cover_url == "https://example.invalid/art.jpg"


def test_missing_tracks_are_flagged_not_deleted(db):
    sync.upsert_tracks(db, _tracks())
    flagged = sync.flag_missing(db, {"B076HFF4Q3"})

    assert flagged == 1
    assert db.query(AmazonMusicTrack).count() == 2  # nothing deleted
    assert db.get(AmazonMusicTrack, "B07BFJR1HC").missing_since is not None
    assert db.get(AmazonMusicTrack, "B076HFF4Q3").missing_since is None


def test_a_track_that_reappears_is_unflagged(db):
    sync.upsert_tracks(db, _tracks())
    sync.flag_missing(db, set())
    assert db.get(AmazonMusicTrack, "B076HFF4Q3").missing_since is not None

    sync.upsert_tracks(db, _tracks())
    assert db.get(AmazonMusicTrack, "B076HFF4Q3").missing_since is None


def test_an_album_with_one_artist_is_not_a_compilation(db):
    sync.upsert_tracks(db, _tracks())
    sync.mark_compilations(db)
    assert db.get(AmazonMusicTrack, "B076HFF4Q3").is_compilation is False


def test_an_album_whose_tracks_disagree_about_the_artist_is_a_compilation(db):
    now = datetime.utcnow()
    for asin, artist in (("T1", "Artist One"), ("T2", "Artist Two")):
        db.add(
            AmazonMusicTrack(
                track_asin=asin, album_asin="COMPILATION", artist=artist, first_seen_at=now, last_seen_at=now
            )
        )
    db.commit()

    sync.mark_compilations(db)
    assert db.get(AmazonMusicTrack, "T1").is_compilation is True
    assert db.get(AmazonMusicTrack, "T2").is_compilation is True


def test_auth_header_field_matches_the_captured_shape():
    field = sync._auth_headers_field({"accessToken": "Atna|EXAMPLE"})
    outer = json.loads(field)
    inner = json.loads(outer["x-amzn-authentication"])

    assert inner["interface"] == "ClientAuthenticationInterface.v1_0.ClientTokenElement"
    assert inner["accessToken"] == "Atna|EXAMPLE"


def test_a_config_without_a_token_is_an_auth_error_not_a_crash():
    with pytest.raises(amc.AmazonMusicAuthError, match="access token"):
        sync._auth_headers_field({})


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_login_raises_a_reconnect_message(status):
    resp = httpx.Response(status, text="denied", request=httpx.Request("GET", "https://x"))
    with patch("httpx.Client.get", return_value=resp):
        with httpx.Client() as client:
            with pytest.raises(amc.AmazonMusicAuthError, match="Reconnect"):
                sync._fetch_config(client)


def test_refresh_requires_a_stored_audible_login(db):
    with pytest.raises(sync.NotConnectedError, match="Audible isn't connected"):
        sync.refresh_purchased_tracks(db)
