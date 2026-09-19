import json
from unittest.mock import patch

import httpx
import pytest

from app.connectors import amazon_music_probe as probe
from app.models.credential import SOURCE_AUDIBLE, STATUS_OK, Credential
from app.security import encrypt_json

SECRET = "Atza|VeryLongSecretCookieValueThatMustNeverAppear"


def _resp(status=200, json_body=None, text="", content_type="application/json"):
    content = json.dumps(json_body).encode() if json_body is not None else text.encode()
    return httpx.Response(
        status,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", probe.CONFIG_URL),
    )


# --------------------------------------------------------------- redaction

def test_redaction_removes_secrets_but_keeps_the_music_schema():
    payload = {
        "customerId": "A1B2C3",
        "csrf": {"token": "tok-abc", "ts": 17},
        "email": "jane@example.com",
        "albums": [
            {
                "asin": "B00XYZ1234",
                "title": "Kind of Blue",
                "artistName": "Miles Davis",
                "purchased": True,
                "durationSeconds": 2685,
                "description": "A studio album recorded in 1959, a landmark of the genre.",
                "opaque": "a" * 80,
                "leaky": f"prefix-{SECRET}-suffix",
            }
        ],
    }
    out = probe.redact(payload, {SECRET})
    blob = json.dumps(out)

    assert SECRET not in blob
    assert "jane@example.com" not in blob
    assert "A1B2C3" not in blob
    assert "tok-abc" not in blob

    album = out["albums"][0]
    assert album["title"] == "Kind of Blue"
    assert album["artistName"] == "Miles Davis"
    assert album["asin"] == "B00XYZ1234"
    assert album["purchased"] is True
    assert album["durationSeconds"] == 2685
    assert album["description"].startswith("A studio album")
    assert album["opaque"].startswith("<REDACTED:opaque")
    assert album["leaky"] == "<REDACTED:contains-secret>"


def test_redaction_keeps_structure_under_a_sensitive_container_key():
    # Blanking the whole subtree would destroy the schema shape the capture
    # exists to reveal, so keys survive even when their values do not.
    out = probe.redact({"csrf": {"token": "x", "ts": 17}}, set())
    assert set(out["csrf"]) == {"token", "ts"}
    assert out["csrf"]["token"] == "<REDACTED:secret>"


def test_signed_in_detection_requires_a_populated_customer_id():
    assert probe.find_signed_in({"a": {"customerId": "A1"}}) is True
    assert probe.find_signed_in({"a": {"customerId": "  "}}) is False
    assert probe.find_signed_in({"albums": []}) is False


def test_key_paths_lists_names_only():
    paths = probe.key_paths({"albums": [{"asin": "B1", "title": "T"}]})
    assert "albums[].asin" in paths
    assert not any("B1" in p or p.endswith(".T") for p in paths)


# ------------------------------------------------------------- curl parsing

def test_parse_curl_extracts_method_headers_cookies_and_body():
    spec = probe.parse_curl(
        "curl 'https://na.mesk.skill.music.a2z.com/api/showLibrary' "
        "-H 'content-type: application/json' -H 'csrf-token: abc' "
        "-H 'cookie: session-id=111; ubid-main=222' "
        "--data-raw '{\"target\":\"showLibrary\"}' --compressed"
    )
    assert spec["method"] == "POST"
    assert spec["headers"]["csrf-token"] == "abc"
    assert "cookie" not in {k.lower() for k in spec["headers"]}
    assert "session-id=111" in spec["cookie_header"]
    assert json.loads(spec["body"])["target"] == "showLibrary"


@pytest.mark.parametrize(
    "url",
    [
        "https://na.mesk.skill.music.a2z.com/api/showLibrary",  # the real API host
        "https://music.amazon.com/config.json",
        "https://a2z.com/x",
    ],
)
def test_parse_curl_accepts_the_hosts_the_web_player_actually_uses(url):
    assert probe.parse_curl(f"curl '{url}'")["url"] == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/collect",
        "https://notamazon.com/collect",  # bare-suffix match would let this through
        "https://a2z.com.evil.net/collect",
        "http://music.amazon.com/config.json",  # plaintext
    ],
)
def test_parse_curl_rejects_anything_else(url):
    with pytest.raises(ValueError, match="other than an https"):
        probe.parse_curl(f"curl '{url}'")


def test_parse_curl_rejects_empty_input():
    with pytest.raises(ValueError):
        probe.parse_curl("   ")


# ------------------------------------------------------------------ probing

def test_probe_config_reports_json_and_signed_in():
    body = {"customerId": "A1", "tier": "unlimited", "albums": []}
    with patch("httpx.Client.get", return_value=_resp(json_body=body)):
        result = probe.probe_config({"at-main": SECRET})

    assert result.ok is True
    assert result.is_json is True
    assert result.signed_in is True
    assert result.top_level_keys == ["albums", "customerId", "tier"]
    assert SECRET not in result.redacted_json
    assert '"customerId": "<REDACTED:identity>"' in result.redacted_json


def test_probe_config_detects_a_waf_challenge():
    html = "<html><head><title>Robot Check</title></head><body>Enter the characters</body></html>"
    with patch("httpx.Client.get", return_value=_resp(text=html, content_type="text/html")):
        result = probe.probe_config({"at-main": SECRET})

    assert result.is_challenge is True
    assert result.is_json is False
    assert result.ok is False


def test_probe_config_raises_a_clean_error_on_transport_failure():
    with patch("httpx.Client.get", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(probe.ProbeError, match="ConnectError"):
            probe.probe_config({})


def test_cookies_require_a_stored_audible_credential(db):
    with pytest.raises(probe.NotConnectedError):
        probe.cookies_from_audible_credential(db)


def test_cookies_reuse_those_already_stored_without_calling_amazon(db):
    db.add(
        Credential(
            source=SOURCE_AUDIBLE,
            status=STATUS_OK,
            encrypted_payload=encrypt_json({"website_cookies": {"at-main": SECRET}, "locale_code": "us"}),
        )
    )
    db.commit()

    class _Auth:
        website_cookies = {"at-main": SECRET}

        @classmethod
        def from_dict(cls, data):
            return cls()

        def set_website_cookies_for_country(self, country):  # pragma: no cover
            raise AssertionError("must not contact Amazon when cookies are already stored")

    with patch("audible.Authenticator", _Auth):
        assert probe.cookies_from_audible_credential(db) == {"at-main": SECRET}


# ------------------------------------------------------------------- routes

def _connect_audible(db):
    db.add(
        Credential(
            source=SOURCE_AUDIBLE,
            status=STATUS_OK,
            encrypted_payload=encrypt_json({"website_cookies": {"at-main": SECRET}, "locale_code": "us"}),
        )
    )
    db.commit()


def test_card_tells_you_to_connect_audible_first(authed_client):
    resp = authed_client.get("/settings")
    assert "Amazon Music" in resp.text
    assert "Connect <strong>Audible</strong> above first" in resp.text


def test_card_offers_the_probe_once_audible_is_connected(authed_client, db):
    _connect_audible(db)
    resp = authed_client.get("/settings")
    assert "Test connection" in resp.text
    assert "/settings/amazon-music/test" in resp.text


def test_test_route_renders_a_json_result(authed_client, db):
    _connect_audible(db)
    result = probe.ProbeResult(
        label="GET /config.json",
        status_code=200,
        content_type="application/json",
        byte_count=42,
        is_json=True,
        is_challenge=False,
        signed_in=True,
        top_level_keys=["customerId", "tier"],
        redacted_json='{"customerId": "<REDACTED:identity>"}',
    )
    with patch.object(probe, "cookies_from_audible_credential", return_value={"at-main": SECRET}), \
         patch.object(probe, "probe_config", return_value=result):
        resp = authed_client.post("/settings/amazon-music/test")

    assert resp.status_code == 200
    assert "Got JSON" in resp.text
    assert SECRET not in resp.text


def test_test_route_surfaces_a_challenge_as_the_stop_condition(authed_client, db):
    _connect_audible(db)
    result = probe.ProbeResult(
        label="GET /config.json",
        status_code=200,
        content_type="text/html",
        byte_count=900,
        is_json=False,
        is_challenge=True,
        signed_in=False,
    )
    with patch.object(probe, "cookies_from_audible_credential", return_value={}), \
         patch.object(probe, "probe_config", return_value=result):
        resp = authed_client.post("/settings/amazon-music/test")

    assert "Challenge page" in resp.text
    assert "stop condition" in resp.text


def test_test_route_reports_a_transport_failure_without_a_500(authed_client, db):
    _connect_audible(db)
    with patch.object(probe, "cookies_from_audible_credential", return_value={}), \
         patch.object(probe, "probe_config", side_effect=probe.ProbeError("Request to config.json failed: ConnectError")):
        resp = authed_client.post("/settings/amazon-music/test")

    assert resp.status_code == 200
    assert "ConnectError" in resp.text


def test_replay_route_rejects_a_bad_paste_without_a_500(authed_client, db):
    _connect_audible(db)
    with patch.object(probe, "cookies_from_audible_credential", return_value={}):
        resp = authed_client.post("/settings/amazon-music/replay", data={"curl_text": "curl 'https://evil.example.com'"})

    assert resp.status_code == 200
    assert "other than an https" in resp.text


def test_probe_routes_reject_a_post_without_a_csrf_token(raw_client, db):
    # raw_client keeps the real require_csrf dependency (the `client` fixture
    # overrides it away), so this is a genuine gate. Log in properly first,
    # otherwise the auth redirect answers before CSRF is ever consulted.
    from app.csrf import COOKIE_NAME

    _connect_audible(db)
    token = raw_client.get("/login").cookies[COOKIE_NAME]
    assert raw_client.post(
        "/login", data={"password": "test-password", "next": "/", "csrf_token": token}, follow_redirects=False
    ).status_code == 303

    assert raw_client.post("/settings/amazon-music/test").status_code == 403
    assert raw_client.post("/settings/amazon-music/replay", data={"curl_text": ""}).status_code == 403


def test_replay_help_text_is_rendered_from_the_real_allowlist(authed_client, db):
    # This drifted once already: the allowlist grew to cover a2z.com (where the
    # web player's API actually lives) while the card still told the user only
    # amazon.com was accepted — i.e. that a valid capture would be rejected.
    # Asserting the real domains appear proves nothing, because the textarea
    # placeholder happens to contain one of them; injecting a domain that
    # exists nowhere else is what actually proves the text comes from the
    # constant rather than being hardcoded alongside it.
    _connect_audible(db)
    sentinel = "example-allowlist-sentinel.test"
    with patch.object(probe, "ALLOWED_REPLAY_DOMAINS", frozenset({sentinel})):
        resp = authed_client.get("/settings")
    assert sentinel in resp.text


# ------------------------------------------------- large-response sampling

def test_sample_lists_keeps_the_schema_and_drops_the_bulk():
    data = {"albums": [{"asin": f"B{i:08d}", "title": f"Album {i}"} for i in range(500)]}
    out = probe.sample_lists(data, max_items=2)

    assert len(out["albums"]) == 3  # two real entries plus the marker
    assert out["albums"][0]["title"] == "Album 0"
    assert out["albums"][1]["title"] == "Album 1"
    assert out["albums"][2] == "<498 more items omitted>"


def test_sample_lists_leaves_short_lists_alone():
    data = {"albums": [{"asin": "B1"}, {"asin": "B2"}]}
    assert probe.sample_lists(data, max_items=3) == data


def test_collection_sizes_reports_counts_not_contents():
    data = {"library": {"albums": [{"asin": "B1", "tracks": [1, 2, 3]}] * 40}}
    sizes = probe.collection_sizes(data)

    assert "library.albums[] = 40" in sizes
    assert any("tracks[] = 3" in s for s in sizes)
    assert not any("B1" in s for s in sizes)


def test_a_large_response_stays_valid_json_instead_of_being_truncated():
    # The real showLibraryAlbums response is ~1.3 MB; slicing the serialised
    # string to a byte budget would return a document cut off mid-object.
    big = {"albums": [{"asin": f"B{i:08d}", "title": "x" * 200} for i in range(3000)]}
    with patch("httpx.Client.get", return_value=_resp(json_body=big)):
        result = probe.probe_config({})

    assert result.is_json is True
    parsed = json.loads(result.redacted_json)  # must not raise
    assert len(parsed["albums"]) == 4
    assert parsed["albums"][-1].endswith("more items omitted>")
    assert "albums[] = 3000" in result.collection_sizes


# ------------------------------------------------------- signed-URL values

SIGNED = (
    "https://d1l04yptno92u8.cloudfront.net/DigitalMusicDeliveryService/CloudDriveEmbed.mp3"
    "?e=1789868665&cid=A2HOWQQO9HOTF7&cdoid=c64eeeb1-e203-4c0a-9213-43ac6202c74a"
    "&isrc=GBAAM8300001&tid=111-1008484-5274644&pt=1566309461277&h=5aa57bcecd5576474b20a63a"
)


def test_a_signed_delivery_url_keeps_its_shape_but_loses_every_value():
    # downloadTrack returns one of these under a key like "url", which matches
    # no sensitive-key pattern — so without this the card would render the
    # customer id, order id and signature in full.
    out = probe.redact({"url": SIGNED}, set())["url"]

    assert "A2HOWQQO9HOTF7" not in out  # customer id
    assert "111-1008484-5274644" not in out  # order id
    assert "5aa57bcecd5576474b20a63a" not in out  # signature
    assert "1566309461277" not in out  # purchase timestamp

    # Names survive: reading them is how the parameters were identified at all.
    assert "cid=<REDACTED>" in out
    assert "isrc=<REDACTED>" in out
    assert "tid=<REDACTED>" in out
    assert out.startswith("https://d1l04yptno92u8.cloudfront.net/DigitalMusicDeliveryService/CloudDriveEmbed.mp3?")


def test_a_url_without_a_query_string_is_left_readable():
    plain = "https://m.media-amazon.com/images/I/81abcdef.jpg"
    assert probe.redact({"coverUrl": plain}, set())["coverUrl"] == plain


def test_a_signed_url_nested_in_a_list_is_redacted_too():
    out = probe.redact({"tracks": [{"downloadUrl": SIGNED}]}, set())
    assert "A2HOWQQO9HOTF7" not in json.dumps(out)


@pytest.mark.parametrize("bad", ["https://host:notaport/?token=abc", "https://b\x00ad/?token=abc"])
def test_an_unparseable_url_shaped_string_is_blanked_rather_than_guessed(bad):
    # httpx.URL is lenient — it percent-encodes most junk rather than raising —
    # but a bad port or a null byte does raise InvalidURL, and the fallback
    # must blank the value instead of letting it through.
    out = probe.redact({"u": bad}, set())["u"]
    assert out == "<REDACTED:url>"
    assert "token=abc" not in out


# ---------------------------------------------------- finding data records

def test_find_record_nodes_locates_records_buried_in_a_ui_template():
    # showPurchasedTracks returns Amazon's server-driven UI format: the tracks
    # sit far below the depth a flat key listing reaches, surrounded by page
    # furniture.
    payload = {
        "methods": [
            {
                "template": {
                    "multiSelectBar": {"actionButton1": {"onItemSelected": []}},
                    "widgets": [
                        {
                            "items": [
                                {"asin": "B001", "title": "A Song", "artistName": "Someone", "durationSeconds": 210},
                                {"asin": "B002", "title": "B Song", "artistName": "Someone", "durationSeconds": 180},
                            ]
                        }
                    ],
                }
            }
        ]
    }
    found = probe.find_record_nodes(payload)

    assert len(found) == 1, "identical shapes should collapse to one"
    path, fields = found[0]
    assert path == "methods[].template.widgets[].items[]"
    assert fields == ["artistName", "asin", "durationSeconds", "title"]


def test_find_record_nodes_ignores_page_furniture():
    furniture = {"multiSelectBar": {"actionButton1": {"onItemSelected": [], "contextMenu": {"options": []}}}}
    assert probe.find_record_nodes(furniture) == []


def test_find_record_nodes_reports_names_never_values():
    payload = {"items": [{"asin": "B001", "title": "Private Title", "isrc": "GB123", "artistName": "X"}]}
    found = probe.find_record_nodes(payload)

    flat = json.dumps(found)
    assert "Private Title" not in flat
    assert "GB123" not in flat
    assert "isrc" in flat


def test_find_record_nodes_distinguishes_different_shapes():
    payload = {
        "tracks": [{"asin": "B1", "title": "T", "isrc": "I", "durationSeconds": 1}],
        "albums": [{"asin": "B2", "albumName": "A", "artistName": "X", "purchased": True}],
    }
    paths = {p for p, _ in probe.find_record_nodes(payload)}
    assert paths == {"tracks[]", "albums[]"}


def test_probe_result_carries_record_shapes():
    body = {"items": [{"asin": "B1", "title": "T", "artistName": "X"}]}
    with patch("httpx.Client.get", return_value=_resp(json_body=body)):
        result = probe.probe_config({})
    assert result.record_shapes == [("items[]", ["artistName", "asin", "title"])]


def test_largest_list_shapes_finds_records_that_use_generic_widget_names():
    # No hint key matches "primaryText"/"secondaryText", but a library's track
    # list is still far longer than any piece of page chrome.
    payload = {
        "template": {
            "contextMenu": {"options": [{"label": "x", "action": "y"}]},
            "widgets": [
                {"items": [{"primaryText": f"Track {i}", "secondaryText": "Artist", "id": f"u{i}"} for i in range(400)]}
            ],
        }
    }
    shapes = probe.largest_list_shapes(payload)
    top_path, top_fields = shapes[0]

    assert "400 items" in top_path
    assert top_fields == ["id", "primaryText", "secondaryText"]
    assert "Track 0" not in json.dumps(shapes)


def test_probe_falls_back_to_list_ranking_when_no_hint_keys_match():
    body = {"widgets": [{"rows": [{"primaryText": "a", "secondaryText": "b", "id": "c"} for _ in range(50)]}]}
    with patch("httpx.Client.get", return_value=_resp(json_body=body)):
        result = probe.probe_config({})

    assert result.record_shapes, "must report something even with no recognisable field names"
    assert "50 items" in result.record_shapes[0][0]


# ------------------------------------------------ endpoints the page can call

def test_referenced_endpoints_maps_the_api_out_of_one_response():
    # In a server-driven UI the payload carries the calls the page can make
    # next — which is how zipDownloadTracks and the sortBy parameter were
    # first spotted by eye.
    payload = {
        "template": {
            "onCreated": [{"url": "https://na.mesk.skill.music.a2z.com/api/showPurchasedTracks?sortBy=X&userHash=Y"}],
            "multiSelectBar": {
                "contextMenu": {
                    "options": [{"url": "https://na.mesk.skill.music.a2z.com/api/zipDownloadTracks?userHash=Y"}]
                }
            },
            "widgets": [
                {"items": [{"url": "https://na.mesk.skill.music.a2z.com/api/thumbsUp?trackCatalogId=Z&userHash=Y"}]}
            ],
        }
    }
    found = probe.referenced_endpoints(payload)

    assert "/api/showPurchasedTracks?sortBy&userHash" in found
    assert "/api/zipDownloadTracks?userHash" in found
    assert "/api/thumbsUp?trackCatalogId&userHash" in found


def test_referenced_endpoints_reports_names_never_values():
    payload = {"url": "https://na.mesk.skill.music.a2z.com/api/x?token=SECRETVALUE&cid=A1B2C3"}
    found = probe.referenced_endpoints(payload)

    assert found == ["/api/x?cid&token"]
    assert "SECRETVALUE" not in json.dumps(found)
    assert "A1B2C3" not in json.dumps(found)


def test_referenced_endpoints_deduplicates_the_same_call_repeated_per_row():
    # elementClicked appears on every one of 50 rows; it is one endpoint.
    same = "https://na.mesk.skill.music.a2z.com/api/elementClicked?info=1&userHash=2"
    payload = {"items": [{"url": same} for _ in range(50)]}
    assert probe.referenced_endpoints(payload) == ["/api/elementClicked?info&userHash"]


def test_probe_result_carries_referenced_endpoints():
    body = {"a": {"url": "https://na.mesk.skill.music.a2z.com/api/showLibraryAlbums?pageToken=abc"}}
    with patch("httpx.Client.get", return_value=_resp(json_body=body)):
        result = probe.probe_config({})
    assert result.endpoints == ["/api/showLibraryAlbums?pageToken"]
