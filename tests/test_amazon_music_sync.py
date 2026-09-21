import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.connectors import amazon_music_connector as amc
from app.connectors import amazon_music_template as tmpl
from app.models.amazon_music_track import AmazonMusicTrack
from app.models.credential import SOURCE_AUDIBLE, STATUS_OK, Credential
from app.security import encrypt_json
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


# ------------------------------------- request shape (the 400 investigation)

def test_page_request_is_json_sent_under_a_text_plain_content_type():
    # Confirmed from a real capture: the body is urlencoded while the
    # Content-Type is text/plain, which is how the web player avoids a CORS
    # preflight. Sending application/x-www-form-urlencoded gets a flat 400.
    captured = {}

    def _fake_post(self, url, **kwargs):
        captured["url"] = url
        captured["content"] = kwargs.get("content")
        captured["headers"] = kwargs.get("headers")
        return httpx.Response(200, json={"methods": []}, request=httpx.Request("POST", url))

    with patch("httpx.Client.post", _fake_post):
        with httpx.Client() as client:
            sync._fetch_page(client, "https://example.invalid", '{"x":"y"}', "{}", "CURSOR")

    assert captured["headers"]["Content-Type"] == "text/plain;charset=UTF-8"
    # JSON, not urlencoded: DevTools rendered the real payload as a quoted
    # tree, and a urlencoded body drew a bare Tomcat 400 from the servlet.
    body = json.loads(captured["content"])
    assert body["sortBy"] == "RECENTLY_ADDED"
    assert body["next"] == "CURSOR"
    assert body["headers"] == '{"x":"y"}'


def test_a_400_surfaces_amazons_own_message():
    resp = httpx.Response(400, text="Missing required header x-amzn-csrf", request=httpx.Request("POST", "https://x"))
    with patch("httpx.Client.post", return_value=resp):
        with httpx.Client() as client:
            with pytest.raises(sync.AmazonMusicRequestError, match="x-amzn-csrf"):
                sync._fetch_page(client, "https://example.invalid", "{}", "{}", "")


def test_session_fields_are_read_off_the_confirmed_config_json_mapping():
    fields = sync._session_fields_from_config(
        {
            "accessToken": "Atna|EXAMPLE",
            "deviceId": "DEV123",
            "deviceType": "TYPE123",
            "sessionId": "SESS123",
            "montanaCsrf": {"token": "abc", "ts": 1},
        }
    )

    assert fields["x-amzn-device-id"] == "DEV123"
    assert fields["x-amzn-device-type-id"] == "TYPE123"
    assert fields["x-amzn-session-id"] == "SESS123"
    # An object csrf is passed through as JSON, not str()-ed into a dict repr.
    assert json.loads(fields["x-amzn-csrf"])["token"] == "abc"
    # accessToken has its own dedicated path (with_fresh_session's envelope
    # swap); it must not also leak in here as a bare header.
    assert "accessToken" not in fields


def test_session_fields_omit_what_config_json_did_not_supply():
    assert sync._session_fields_from_config({"accessToken": "Atna|EXAMPLE"}) == {}


# --------------------------------------------- zero-track diagnostics

def test_diagnose_empty_page_reports_names_paths_and_counts_only():
    payload = {
        "customerId": "SHOULD-NEVER-APPEAR",
        "methods": [
            {
                "template": {
                    "widgets": [{"rows": [{"primaryText": "x", "secondaryText": "y"} for _ in range(12)]}],
                    "multiSelectBar": {"actionButton1": {"onItemSelected": []}},
                }
            }
        ],
    }
    diag = sync._diagnose_empty_page(payload)

    assert diag["top_level_keys"] == ["customerId", "methods"]
    assert any("rows[] = 12" in s for s in diag["collection_sizes"])
    # No semantic field names here, so this must fall back to the largest
    # list rather than come back empty.
    assert diag["record_shapes"]
    assert "SHOULD-NEVER-APPEAR" not in json.dumps(diag)


def test_diagnose_empty_page_on_a_non_dict_payload_does_not_crash():
    diag = sync._diagnose_empty_page([1, 2, 3])
    assert diag["top_level_keys"] == []


def _connect_audible_for_sync(db):
    db.add(
        Credential(
            source=SOURCE_AUDIBLE,
            status=STATUS_OK,
            encrypted_payload=encrypt_json({"website_cookies": {"at-main": "x"}, "locale_code": "us"}),
        )
    )
    db.commit()


class _Auth:
    website_cookies = {"at-main": "x"}

    @classmethod
    def from_dict(cls, data):
        return cls()


def _config_response():
    return httpx.Response(
        200, json={"accessToken": "Atna|NEW"}, request=httpx.Request("GET", "https://music.amazon.com/config.json")
    )


def test_refresh_reports_a_diagnostic_when_the_page_has_no_tracks(db):
    _connect_audible_for_sync(db)
    tmpl.save_template(
        db,
        tmpl.SyncTemplate(
            path=amc.PURCHASED_TRACKS_PATH,
            headers_field=json.dumps({"x-amzn-authentication": json.dumps({"accessToken": "OLD"})}),
            user_hash="{}",
            captured_at="now",
        ),
    )

    no_tracks_page = httpx.Response(
        200,
        json={"methods": [{"template": {"widgets": [{"rows": [{"a": 1}, {"a": 2}]}]}}]},
        request=httpx.Request("POST", "https://x/api/showPurchasedTracks"),
    )

    with patch("audible.Authenticator", _Auth), patch("httpx.Client.get", return_value=_config_response()), patch(
        "httpx.Client.post", return_value=no_tracks_page
    ):
        result = sync.refresh_purchased_tracks(db)

    assert result["tracks_seen"] == 0
    assert "diagnostic" in result
    assert result["diagnostic"]["record_shapes"]


def test_refresh_reports_no_diagnostic_when_tracks_are_found(db):
    _connect_audible_for_sync(db)
    tmpl.save_template(
        db,
        tmpl.SyncTemplate(
            path=amc.PURCHASED_TRACKS_PATH,
            headers_field=json.dumps({"x-amzn-authentication": json.dumps({"accessToken": "OLD"})}),
            user_hash="{}",
            captured_at="now",
        ),
    )

    real_page = httpx.Response(
        200,
        json=json.loads(FIXTURE.read_text(encoding="utf-8")),
        request=httpx.Request("POST", "https://x/api/showPurchasedTracks"),
    )
    # The fixture embeds a `next` cursor (it exists to prove pagination
    # parsing works), so a mock returning it on every call would loop until
    # MAX_PAGES with a real sleep between each — a second, cursor-free page
    # is what lets this test terminate after two calls instead of ~400.
    last_page = httpx.Response(
        200, json={"methods": []}, request=httpx.Request("POST", "https://x/api/showPurchasedTracks")
    )

    with patch("audible.Authenticator", _Auth), patch("httpx.Client.get", return_value=_config_response()), patch(
        "httpx.Client.post", side_effect=[real_page, last_page]
    ), patch("time.sleep"):
        result = sync.refresh_purchased_tracks(db)

    assert result["pages"] == 2
    assert result["tracks_seen"] == 2
    assert "diagnostic" not in result


def test_refresh_reports_candidates_and_unmatched_shapes_for_a_low_yield_page(db):
    # This is the real symptom that motivated the yield diagnostics: a page
    # that returns some tracks (not zero, so the empty-page diagnostic never
    # fires) but drops most of its rows, with no prior way to see that it
    # happened at all.
    _connect_audible_for_sync(db)
    tmpl.save_template(
        db,
        tmpl.SyncTemplate(
            path=amc.PURCHASED_TRACKS_PATH,
            headers_field=json.dumps({"x-amzn-authentication": json.dumps({"accessToken": "OLD"})}),
            user_hash="{}",
            captured_at="now",
        ),
    )

    low_yield_page = httpx.Response(
        200,
        json={
            "methods": [
                {
                    "template": {
                        "widgets": [
                            {
                                "items": [
                                    {
                                        "primaryText": "Matches",
                                        "button": {
                                            "observer": {
                                                "storageKey": "B076HFF4Q3",
                                                "storageGroup": "TRACK_RATINGS",
                                            }
                                        },
                                        "onCheckboxSelected": {"states": {}},
                                    },
                                    {"primaryText": "No asin here", "onCheckboxSelected": {"states": {}}},
                                    {"primaryText": "Also no asin", "onCheckboxSelected": {"states": {}}},
                                ]
                            }
                        ]
                    }
                }
            ]
        },
        request=httpx.Request("POST", "https://x/api/showPurchasedTracks"),
    )

    with patch("audible.Authenticator", _Auth), patch("httpx.Client.get", return_value=_config_response()), patch(
        "httpx.Client.post", return_value=low_yield_page
    ):
        result = sync.refresh_purchased_tracks(db)

    assert result["tracks_seen"] == 1
    assert result["candidates_seen"] == 3
    assert "diagnostic" not in result  # tracks were found — the total-failure path is a different case
    assert result["unmatched_item_shapes"] == [["onCheckboxSelected", "primaryText"]]


def test_refresh_reports_no_unmatched_shapes_when_every_row_matches(db):
    _connect_audible_for_sync(db)
    tmpl.save_template(
        db,
        tmpl.SyncTemplate(
            path=amc.PURCHASED_TRACKS_PATH,
            headers_field=json.dumps({"x-amzn-authentication": json.dumps({"accessToken": "OLD"})}),
            user_hash="{}",
            captured_at="now",
        ),
    )

    all_match_page = httpx.Response(
        200,
        json={
            "items": [
                {
                    "primaryText": "T",
                    "button": {"observer": {"storageKey": "B076HFF4Q3", "storageGroup": "TRACK_RATINGS"}},
                    "onCheckboxSelected": {"states": {}},
                }
            ]
        },
        request=httpx.Request("POST", "https://x/api/showPurchasedTracks"),
    )

    with patch("audible.Authenticator", _Auth), patch("httpx.Client.get", return_value=_config_response()), patch(
        "httpx.Client.post", return_value=all_match_page
    ):
        result = sync.refresh_purchased_tracks(db)

    assert result["candidates_seen"] == 1
    assert result["tracks_seen"] == 1
    assert "unmatched_item_shapes" not in result
