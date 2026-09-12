from datetime import datetime, timezone

from app.entitlement_status import owned_title_sets, parse_expiration
from app.models.gog_game import GogGame
from app.models.steam_game import SteamGame


def test_parse_expiration_returns_none_when_no_expiration_field():
    assert parse_expiration({}) is None


def test_parse_expiration_returns_none_for_malformed_date():
    assert parse_expiration({"expiration_date": "not-a-date"}) is None


def test_parse_expiration_parses_expiration_date_field():
    assert parse_expiration({"expiration_date": "2026-01-01T00:00:00"}) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_parse_expiration_falls_back_to_expiry_date_field():
    assert parse_expiration({"expiry_date": "2026-01-01T00:00:00"}) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_parse_expiration_prefers_expiration_date_over_expiry_date():
    raw = {"expiration_date": "2026-01-01T00:00:00", "expiry_date": "2027-01-01T00:00:00"}
    assert parse_expiration(raw) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_owned_title_sets_returns_casefolded_names_from_each_library(db):
    db.add(SteamGame(appid=1, name="Half-Life 2", playtime_forever_minutes=0, img_icon_url=""))
    db.add(GogGame(product_id=1, title="Shadowrun Returns", image_url=""))
    db.commit()

    steam_titles, gog_titles = owned_title_sets(db)
    assert steam_titles == {"half-life 2"}
    assert gog_titles == {"shadowrun returns"}


def test_owned_title_sets_empty_when_nothing_synced(db):
    assert owned_title_sets(db) == (set(), set())
