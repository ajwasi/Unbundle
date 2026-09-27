import json
from pathlib import Path

import pytest

from app.connectors import amazon_music_connector as amc

FIXTURE = Path(__file__).parent / "fixtures" / "amazon_music_purchased_tracks.json"


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parses_every_row_that_carries_a_download_id(payload):
    tracks = amc.parse_tracks(payload)
    # The third fixture row has no download_id (empty onCheckboxSelected
    # .states) and must be dropped, not stored under a guessed identity.
    assert [t.track_asin for t in tracks] == ["B076HFF4Q3", "B07BFJR1HC"]


def test_maps_the_presentation_slots_to_named_fields(payload):
    track = amc.parse_tracks(payload)[0]

    assert track.title == "Every Breath You Take"
    assert track.artist == "The Police"
    assert track.album == "Synchronicity"
    assert track.duration_display == "4:13"
    assert track.duration_seconds == 253


def test_recovers_asins_from_the_row_deeplinks(payload):
    track = amc.parse_tracks(payload)[0]
    assert track.artist_asin == "B000RHPRUY"
    assert track.album_asin == "B074JM9JHY"


def test_recovers_the_download_uuid_which_is_a_key_not_a_value(payload):
    tracks = amc.parse_tracks(payload)
    assert tracks[0].download_id == "c64eeeb1-e203-4c0a-9213-43ac6202c74a"
    assert tracks[1].download_id == "a941e672-3d37-43df-9c99-3232473fdf27"


def test_picks_up_the_presigned_cover_url(payload):
    tracks = amc.parse_tracks(payload)
    assert tracks[0].cover_url.startswith("https://m.media-amazon.com/images/")
    assert tracks[1].cover_url == ""  # blank image, no fallback invented


def test_reads_the_pagination_cursor_off_the_embedded_url(payload):
    assert amc.parse_next_cursor(payload) == "tztok-v0__EXAMPLE="


def test_no_cursor_means_last_page():
    assert amc.parse_next_cursor({"methods": []}) == ""


@pytest.mark.parametrize(
    "display,expected",
    [("3:57", 237), ("4:13", 253), ("1:02:03", 3723), ("0:09", 9), ("12:34:56", 45296)],
)
def test_duration_parsing(display, expected):
    assert amc.parse_duration(display) == expected


@pytest.mark.parametrize("display", ["", "not a duration", "Explicit", "3", "1:2:3:4", "99"])
def test_duration_returns_none_rather_than_guessing(display):
    # secondaryText3 is a positional display slot; a row carrying something
    # else there must not become a bogus number.
    assert amc.parse_duration(display) is None


def test_tracks_are_found_even_if_the_widget_tree_gains_a_wrapper(payload):
    # Parsing walks for `items` rather than indexing the confirmed path, so an
    # extra level does not silently empty the sync.
    wrapped = {"outer": {"extra": payload}}
    assert len(amc.parse_tracks(wrapped)) == 2


def test_duplicate_rows_collapse_by_download_id(payload):
    # The exact same rows appearing twice (e.g. an overlapping page boundary)
    # share their download_id too, so they still collapse to 2 — this is
    # legitimate duplicate-row dedup, distinct from the bug where *different*
    # rows wrongly shared an ASIN (see test_rows_sharing_an_asin_no_longer_
    # collapse_to_one_track for that case).
    doubled = {"a": payload, "b": json.loads(FIXTURE.read_text(encoding="utf-8"))}
    assert len(amc.parse_tracks(doubled)) == 2


def test_demo_mode_points_at_the_mock_api(monkeypatch):
    monkeypatch.setattr("app.config.settings.demo_mode", True)
    monkeypatch.setattr("app.config.settings.mock_api_base_url", "http://mock-api:8090")
    assert amc.api_base() == "http://mock-api:8090/amazon-music"


def test_live_mode_points_at_amazon(monkeypatch):
    monkeypatch.setattr("app.config.settings.demo_mode", False)
    assert amc.api_base() == amc.API_HOST


# ------------------------------------------------------- yield diagnostics

def test_parse_tracks_with_yield_reports_the_true_denominator(payload):
    # A headline "2 tracks" cannot tell "this page only had 2 rows" apart
    # from "this page had many rows and only 2 were recognised" — candidates
    # is what makes that distinction visible.
    tracks, candidates, unmatched = amc.parse_tracks_with_yield(payload)

    assert [t.track_asin for t in tracks] == ["B076HFF4Q3", "B07BFJR1HC"]
    assert candidates == 3  # all three fixture rows, matched or not
    assert unmatched == [["componentType", "onCheckboxSelected", "primaryText", "rowIndex", "secondaryText1", "secondaryText2", "secondaryText3"]]


def test_unmatched_shapes_report_names_never_values(payload):
    flat = json.dumps(amc.parse_tracks_with_yield(payload)[2])
    # The unmatched row's own primaryText is "A Row With No Asin" — a key
    # name report must not also carry that value along with it.
    assert "A Row With No Asin" not in flat


def test_unmatched_shapes_deduplicate_and_respect_the_limit():
    # Twenty identical non-track rows are one shape, not twenty repeats of it.
    payload = {"items": [{"foo": 1, "bar": 2} for _ in range(20)]}
    _tracks, candidates, unmatched = amc.parse_tracks_with_yield(payload, unmatched_limit=5)

    assert candidates == 20
    assert unmatched == [["bar", "foo"]]


def test_a_page_with_nothing_but_matching_rows_reports_no_unmatched_shapes():
    payload = {
        "items": [
            {
                "primaryText": "T",
                "button": {"observer": {"storageKey": "B076HFF4Q3", "storageGroup": "TRACK_RATINGS"}},
                "onCheckboxSelected": {"states": {"c64eeeb1-e203-4c0a-9213-43ac6202c74a": {}}},
            }
        ]
    }
    tracks, candidates, unmatched = amc.parse_tracks_with_yield(payload)
    assert len(tracks) == 1
    assert candidates == 1
    assert unmatched == []


def test_parse_tracks_is_unchanged_by_the_new_wrapper(payload):
    # parse_tracks() must keep returning exactly what it always did — only
    # the internals moved into parse_tracks_with_yield().
    assert amc.parse_tracks(payload) == amc.parse_tracks_with_yield(payload)[0]


def test_rows_sharing_an_asin_no_longer_collapse_to_one_track():
    # The real bug this guards: a live sync where 10,000 rows, each with its
    # own distinct download_id, all carried the same handful of storage-key
    # ASINs — dedupe used to key on that ASIN and silently collapsed them.
    # Identity is download_id now, so three rows with distinct download_ids
    # stay three tracks even though they share one ASIN.
    def row(asin: str, download_uuid: str) -> dict:
        return {
            "primaryText": "Some Title",
            "button": {"observer": {"storageGroup": "TRACK_RATINGS", "storageKey": asin}},
            "onCheckboxSelected": {"states": {download_uuid: {}}},
        }

    payload = {
        "items": [
            row("B076HFF4Q3", "c64eeeb1-e203-4c0a-9213-43ac6202c74a"),
            row("B076HFF4Q3", "a941e672-3d37-43df-9c99-3232473fdf27"),
            row("B076HFF4Q3", "11111111-1111-1111-1111-111111111111"),
        ]
    }

    tracks, candidates, unmatched = amc.parse_tracks_with_yield(payload)

    assert candidates == 3
    assert len(tracks) == 3  # no longer collapsed by the shared ASIN
    assert {t.download_id for t in tracks} == {
        "c64eeeb1-e203-4c0a-9213-43ac6202c74a",
        "a941e672-3d37-43df-9c99-3232473fdf27",
        "11111111-1111-1111-1111-111111111111",
    }
    assert {t.track_asin for t in tracks} == {"B076HFF4Q3"}  # kept, just not identity
    assert unmatched == []


def test_a_row_with_no_download_id_is_dropped_even_with_an_asin():
    # download_id, not the ASIN, is now the row's identity — a row that has
    # an ASIN but no download_id has nothing to key storage on and must be
    # dropped, mirroring the old ASIN-based rule it replaced.
    payload = {
        "items": [
            {
                "primaryText": "No Download Id",
                "button": {"observer": {"storageGroup": "TRACK_RATINGS", "storageKey": "B076HFF4Q3"}},
                "onCheckboxSelected": {"states": {}},
            }
        ]
    }

    tracks, candidates, unmatched = amc.parse_tracks_with_yield(payload)

    assert tracks == []
    assert candidates == 1
    assert unmatched == [["button", "onCheckboxSelected", "primaryText"]]


# ------------------------------------------------------ signed download URL

_SIGNED_URL = (
    "https://d1l04yptno92u8.cloudfront.net/DigitalMusicDeliveryService/CloudDriveEmbed.mp3"
    "?e=1789868665&cid=A2HOWQQO9HOTF7&cdoid=c64eeeb1-e203-4c0a-9213-43ac6202c74a"
    "&isrc=GBAAM8300001&tid=111-1008484-5274644&pt=1566309461277&h=5aa57bcecd5576474b20a63a"
)


def test_find_signed_download_url_locates_it_regardless_of_wrapping_key():
    # The wrapping key was only ever eyeballed as "something like 'url'", never
    # pinned down structurally — this must find it under any key name.
    assert amc.find_signed_download_url({"someUnexpectedKey": _SIGNED_URL}) == _SIGNED_URL


def test_find_signed_download_url_ignores_unrelated_urls():
    payload = {"coverUrl": "https://m.media-amazon.com/images/I/example.jpg", "nested": {"url": _SIGNED_URL}}
    assert amc.find_signed_download_url(payload) == _SIGNED_URL


def test_find_signed_download_url_returns_empty_when_absent():
    assert amc.find_signed_download_url({"template": {"closeButton": {}}}) == ""
