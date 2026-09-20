import json
from unittest.mock import patch

import pytest

from app.connectors import amazon_music_probe as probe
from app.connectors import amazon_music_template as tmpl
from app.models.credential import SOURCE_AMAZON_MUSIC, SOURCE_AUDIBLE, STATUS_OK, Credential
from app.security import encrypt_json
from app.sync import amazon_music_sync as sync

AUTH = json.dumps(
    {
        "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement",
        "accessToken": "Atna|OLD-TOKEN",
        "expirationMS": 1789867018924,
    }
)
HEADERS_FIELD = json.dumps(
    {
        "x-amzn-authentication": AUTH,
        "x-amzn-device-id": "DEV123",
        "x-amzn-device-type-id": "TYPE123",
        "x-amzn-feature-flags": "hd-supported",
    }
)


def _curl(path="/api/showPurchasedTracks", body=None):
    body = body if body is not None else json.dumps(
        {"headers": HEADERS_FIELD, "sortBy": "RECENTLY_ADDED", "userHash": '{"level":"SONIC_RUSH_MEMBER"}'}
    )
    return (
        f"curl 'https://na.web.skill.music.a2z.com{path}' "
        "-H 'content-type: text/plain;charset=UTF-8' "
        f"--data-raw '{body}'"
    )


# ------------------------------------------------------------ building it


def test_builds_a_template_from_a_real_capture():
    t = tmpl.build_template(_curl(), "2026-09-20 12:00 UTC")

    assert t.path == "/api/showPurchasedTracks"
    assert t.user_hash == '{"level":"SONIC_RUSH_MEMBER"}'
    assert "x-amzn-device-id" in t.headers_field


def test_a_urlencoded_capture_is_also_accepted():
    # A capture from a different or older client encodes the same fields
    # differently; rejecting it would be unhelpful when they are right there.
    from urllib.parse import urlencode

    body = urlencode({"headers": HEADERS_FIELD, "sortBy": "RECENTLY_ADDED", "userHash": "{}"})
    t = tmpl.build_template(_curl(body=body), "now")
    assert "x-amzn-device-id" in t.headers_field


def test_a_capture_for_the_wrong_endpoint_is_rejected():
    with pytest.raises(tmpl.TemplateError, match="showPurchasedTracks"):
        tmpl.build_template(_curl(path="/api/elementClicked"), "now")


def test_a_capture_without_an_auth_field_is_rejected():
    body = json.dumps({"headers": json.dumps({"x-amzn-device-id": "D"}), "userHash": "{}"})
    with pytest.raises(tmpl.TemplateError, match="x-amzn-authentication"):
        tmpl.build_template(_curl(body=body), "now")


def test_a_capture_with_no_body_is_rejected():
    with pytest.raises(tmpl.TemplateError, match="no request body"):
        tmpl.build_template("curl 'https://na.web.skill.music.a2z.com/api/showPurchasedTracks'", "now")


# ------------------------------------------------------- refreshing it


def test_only_the_access_token_is_replaced():
    out = json.loads(tmpl.with_fresh_token(HEADERS_FIELD, "Atna|NEW-TOKEN"))

    # Every captured field survives untouched — those are the ones whose
    # necessity is undocumented, which is the whole reason for the template.
    assert out["x-amzn-device-id"] == "DEV123"
    assert out["x-amzn-device-type-id"] == "TYPE123"
    assert out["x-amzn-feature-flags"] == "hd-supported"

    envelope = json.loads(out["x-amzn-authentication"])
    assert envelope["accessToken"] == "Atna|NEW-TOKEN"
    assert envelope["interface"] == "ClientAuthenticationInterface.v1_0.ClientTokenElement"


def test_the_captured_expiry_is_dropped_rather_than_carried_over():
    # expirationMS describes the OLD token; keeping it would be a lie about
    # the new one.
    envelope = json.loads(json.loads(tmpl.with_fresh_token(HEADERS_FIELD, "NEW"))["x-amzn-authentication"])
    assert "expirationMS" not in envelope


def test_the_old_token_never_survives_a_refresh():
    assert "OLD-TOKEN" not in tmpl.with_fresh_token(HEADERS_FIELD, "NEW")


def test_an_unreadable_template_asks_for_a_fresh_capture():
    with pytest.raises(tmpl.TemplateError, match="fresh capture"):
        tmpl.with_fresh_token("not json at all", "NEW")


# ----------------------------------------------------------- persistence


def test_save_and_load_round_trip(db):
    t = tmpl.build_template(_curl(), "2026-09-20 12:00 UTC")
    tmpl.save_template(db, t)

    loaded = tmpl.load_template(db)
    assert loaded.headers_field == t.headers_field
    assert loaded.captured_at == "2026-09-20 12:00 UTC"


def test_nothing_saved_loads_as_none(db):
    assert tmpl.load_template(db) is None


def test_clearing_removes_it(db):
    tmpl.save_template(db, tmpl.build_template(_curl(), "now"))
    assert tmpl.clear_template(db) is True
    assert tmpl.load_template(db) is None


def test_the_stored_blob_is_encrypted_at_rest(db):
    tmpl.save_template(db, tmpl.build_template(_curl(), "now"))
    cred = Credential.get(db, SOURCE_AMAZON_MUSIC)

    # It carries device identifiers, so it must not sit in the clear.
    assert b"DEV123" not in cred.encrypted_payload
    assert b"Atna" not in cred.encrypted_payload


# ----------------------------------------------------------------- sync


@pytest.mark.asyncio
async def test_sync_without_a_template_says_what_to_do(db):
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

    with patch("audible.Authenticator", _Auth):
        with pytest.raises(sync.NoTemplateError, match="No sync template saved"):
            sync.refresh_purchased_tracks(db)


# ------------------------------------------------------------- settings


def _connect_audible(db):
    db.add(
        Credential(
            source=SOURCE_AUDIBLE,
            status=STATUS_OK,
            encrypted_payload=encrypt_json({"website_cookies": {"at-main": "x"}, "locale_code": "us"}),
        )
    )
    db.commit()


def _ok_result():
    return probe.ProbeResult(
        label="POST /api/showPurchasedTracks",
        status_code=200,
        content_type="application/json",
        byte_count=3552060,
        is_json=True,
        is_challenge=False,
        signed_in=True,
    )


def _challenge_result():
    return probe.ProbeResult(
        label="POST /api/showPurchasedTracks",
        status_code=200,
        content_type="text/html",
        byte_count=900,
        is_json=False,
        is_challenge=True,
        signed_in=False,
    )


def test_saving_a_template_verifies_it_first(authed_client, db):
    _connect_audible(db)
    with patch.object(probe, "cookies_from_audible_credential", return_value={"at-main": "x"}), patch.object(
        probe, "replay_curl", return_value=_ok_result()
    ):
        resp = authed_client.post("/settings/amazon-music/template", data={"curl_text": _curl()})

    assert resp.status_code == 200
    assert tmpl.load_template(db) is not None


def test_a_capture_that_does_not_work_is_not_saved(authed_client, db):
    # Storing an unverified template would trade one silent failure for
    # another, harder to see.
    _connect_audible(db)
    with patch.object(probe, "cookies_from_audible_credential", return_value={}), patch.object(
        probe, "replay_curl", return_value=_challenge_result()
    ):
        resp = authed_client.post("/settings/amazon-music/template", data={"curl_text": _curl()})

    assert "not saved" in resp.text
    assert tmpl.load_template(db) is None


def test_a_bad_paste_explains_rather_than_500s(authed_client, db):
    _connect_audible(db)
    resp = authed_client.post("/settings/amazon-music/template", data={"curl_text": "not a curl command"})

    assert resp.status_code == 200
    assert tmpl.load_template(db) is None


def test_the_card_shows_only_the_capture_date_never_the_template(authed_client, db):
    _connect_audible(db)
    tmpl.save_template(db, tmpl.build_template(_curl(), "2026-09-20 12:00 UTC"))

    resp = authed_client.get("/settings")
    assert "2026-09-20 12:00 UTC" in resp.text
    assert "DEV123" not in resp.text
    assert "Atna" not in resp.text


def test_clearing_the_template_from_settings(authed_client, db):
    _connect_audible(db)
    tmpl.save_template(db, tmpl.build_template(_curl(), "now"))

    authed_client.post("/settings/amazon-music/template/clear")
    assert tmpl.load_template(db) is None
