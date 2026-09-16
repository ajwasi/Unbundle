from datetime import datetime

from app.list_views import apply_range_filter, parse_optional_date, parse_optional_float
from app.models.steam_game import SteamGame


def test_apply_range_filter_min_only(db):
    db.add(SteamGame(appid=1, name="Short", playtime_forever_minutes=10))
    db.add(SteamGame(appid=2, name="Long", playtime_forever_minutes=1000))
    db.commit()

    rows = apply_range_filter(db.query(SteamGame), SteamGame.playtime_forever_minutes, 500, None).all()
    assert [r.name for r in rows] == ["Long"]


def test_apply_range_filter_max_only(db):
    db.add(SteamGame(appid=1, name="Short", playtime_forever_minutes=10))
    db.add(SteamGame(appid=2, name="Long", playtime_forever_minutes=1000))
    db.commit()

    rows = apply_range_filter(db.query(SteamGame), SteamGame.playtime_forever_minutes, None, 500).all()
    assert [r.name for r in rows] == ["Short"]


def test_apply_range_filter_min_and_max(db):
    db.add(SteamGame(appid=1, name="Short", playtime_forever_minutes=10))
    db.add(SteamGame(appid=2, name="Medium", playtime_forever_minutes=500))
    db.add(SteamGame(appid=3, name="Long", playtime_forever_minutes=1000))
    db.commit()

    rows = apply_range_filter(db.query(SteamGame), SteamGame.playtime_forever_minutes, 100, 600).all()
    assert [r.name for r in rows] == ["Medium"]


def test_apply_range_filter_neither_bound_is_a_noop(db):
    db.add(SteamGame(appid=1, name="Short", playtime_forever_minutes=10))
    db.add(SteamGame(appid=2, name="Long", playtime_forever_minutes=1000))
    db.commit()

    rows = apply_range_filter(db.query(SteamGame), SteamGame.playtime_forever_minutes, None, None).all()
    assert len(rows) == 2


def test_parse_optional_float_valid_value():
    assert parse_optional_float("12.5") == 12.5


def test_parse_optional_float_blank_is_none():
    assert parse_optional_float("") is None
    assert parse_optional_float("   ") is None


def test_parse_optional_float_garbage_is_none_not_a_crash():
    assert parse_optional_float("not-a-number") is None


def test_parse_optional_date_valid_value():
    assert parse_optional_date("2024-03-15") == datetime(2024, 3, 15)


def test_parse_optional_date_blank_is_none():
    assert parse_optional_date("") is None


def test_parse_optional_date_garbage_is_none_not_a_crash():
    assert parse_optional_date("not-a-date") is None
