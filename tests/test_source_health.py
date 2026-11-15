from datetime import timedelta
from unittest.mock import AsyncMock, patch

from app.models.credential import SOURCE_PLEX, SOURCE_STEAM, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.models.source_health import SourceHealth
from app.services import source_health as sh
from app.timeutil import utcnow


def _credential(db, source, status, error=None):
    cred = Credential.get_or_create(db, source)
    cred.status = status
    cred.last_error = error
    db.commit()


# ------------------------------------------------------------------- recording


def test_a_failure_counts_up_and_keeps_the_last_success(db):
    sh.record_success(db, sh.SOURCE_STOREFRONT)
    first_ok = sh.get(db, sh.SOURCE_STOREFRONT).last_success_at

    sh.record_failure(db, sh.SOURCE_STOREFRONT, ValueError("markup changed"))
    sh.record_failure(db, sh.SOURCE_STOREFRONT, ValueError("markup changed again"))

    row = sh.get(db, sh.SOURCE_STOREFRONT)
    assert row.consecutive_failures == 2
    assert row.last_error == "markup changed again"
    assert row.last_failure_at is not None
    assert row.last_success_at == first_ok  # "last worked" survives the outage


def test_a_success_clears_the_failure_state(db):
    sh.record_failure(db, sh.SOURCE_STOREFRONT, "boom")
    sh.record_success(db, sh.SOURCE_STOREFRONT)

    row = sh.get(db, sh.SOURCE_STOREFRONT)
    assert row.consecutive_failures == 0 and row.last_error is None and row.last_success_at is not None


def test_a_healthy_source_is_not_rewritten_on_every_request(db):
    long_ago = utcnow() - timedelta(minutes=3)
    sh.record_success(db, sh.SOURCE_STOREFRONT, at=long_ago)

    sh.record_success(db, sh.SOURCE_STOREFRONT)  # 3 minutes later: still fresh enough

    assert sh.get(db, sh.SOURCE_STOREFRONT).last_success_at == long_ago


def test_success_reports_when_it_actually_happened_not_when_it_was_noticed(db):
    fetched = utcnow() - timedelta(minutes=25)  # e.g. a cached listing
    sh.record_success(db, sh.SOURCE_STOREFRONT, at=fetched)
    assert sh.get(db, sh.SOURCE_STOREFRONT).last_success_at == fetched


def test_a_very_long_error_is_truncated(db):
    sh.record_failure(db, sh.SOURCE_STOREFRONT, "x" * 5000)
    assert len(sh.get(db, sh.SOURCE_STOREFRONT).last_error) == 1000


# -------------------------------------------------------------------- overview


def _by_label(rows):
    return {r["label"]: r for r in rows}


def test_overview_shows_configured_sources_only_with_their_state(db):
    _credential(db, SOURCE_STEAM, STATUS_OK)
    _credential(db, SOURCE_PLEX, STATUS_ERROR, "token rejected")
    rows = _by_label(sh.overview(db))

    assert rows["Steam"]["state"] == "ok" and rows["Steam"]["last_ok"] is not None
    assert rows["Plex"]["state"] == "error" and rows["Plex"]["detail"] == "token rejected"
    assert "GOG" not in rows  # never configured: not a problem worth a row


def test_overview_ignores_a_credential_that_was_never_set_up(db):
    _credential(db, SOURCE_STEAM, STATUS_NOT_CONFIGURED)
    assert "Steam" not in _by_label(sh.overview(db))


def test_overview_covers_the_public_storefront_through_all_three_states(db):
    label = "Humble storefront (bundles for sale)"

    assert _by_label(sh.overview(db))[label]["state"] == "off"  # not checked yet

    sh.record_success(db, sh.SOURCE_STOREFRONT)
    assert _by_label(sh.overview(db))[label]["state"] == "ok"

    sh.record_failure(db, sh.SOURCE_STOREFRONT, "listing changed")
    row = _by_label(sh.overview(db))[label]
    assert row["state"] == "error" and row["detail"] == "listing changed"
    assert row["last_ok"] is not None  # and it still says when it last worked


# ------------------------------------------------------------------- the pages


def test_home_records_a_failed_listing_and_says_when_it_last_worked(authed_client, db):
    sh.record_success(db, sh.SOURCE_STOREFRONT, at=utcnow() - timedelta(days=2))

    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(side_effect=ValueError("markup changed"))):
        resp = authed_client.get("/")

    assert resp.status_code == 200
    assert "bundle listing right now" in resp.text
    assert "It last worked" in resp.text and "2 days ago" in resp.text
    db.expire_all()
    assert db.get(SourceHealth, sh.SOURCE_STOREFRONT).consecutive_failures == 1


def test_home_records_a_good_listing_as_working(authed_client, db):
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=[])):
        authed_client.get("/")

    db.expire_all()
    row = db.get(SourceHealth, sh.SOURCE_STOREFRONT)
    assert row.consecutive_failures == 0 and row.last_success_at is not None


def test_settings_page_shows_the_source_health_card(authed_client, db):
    _credential(db, SOURCE_STEAM, STATUS_ERROR, "API key rejected")
    html = authed_client.get("/settings").text

    assert 'id="source-health-card"' in html
    assert "Humble storefront (bundles for sale)" in html
    assert "badge-error" in html and "API key rejected" in html


# ------------------------------------------- the other scrapes report in too


def test_scrapes_nobody_has_used_are_not_listed(db):
    labels = _by_label(sh.overview(db))
    assert "GOG deals (catalogue)" not in labels and "Chirp wishlist" not in labels


def test_gog_deals_outcomes_are_recorded(authed_client, db):
    with patch("app.routers.deals.gog_deals.fetch_deals", new=AsyncMock(side_effect=ValueError("catalogue changed"))):
        authed_client.get("/deals/gog")
    row = _by_label(sh.overview(db))["GOG deals (catalogue)"]
    assert row["state"] == "error" and row["detail"] == "catalogue changed"

    with patch("app.routers.deals.gog_deals.fetch_deals", new=AsyncMock(return_value=([], 0))):
        authed_client.get("/deals/gog")
    assert _by_label(sh.overview(db))["GOG deals (catalogue)"]["state"] == "ok"


def test_a_store_wishlist_refresh_is_recorded_either_way(authed_client, db):
    with patch("app.routers.wishlist.store_wishlist_sync.refresh_steam_wishlist", new=AsyncMock(side_effect=RuntimeError("boom"))):
        authed_client.post("/wishlist/steam/refresh")
    assert _by_label(sh.overview(db))["Steam wishlist"]["state"] == "error"

    with patch("app.routers.wishlist.store_wishlist_sync.refresh_steam_wishlist", new=AsyncMock(return_value={})):
        authed_client.post("/wishlist/steam/refresh")
    assert _by_label(sh.overview(db))["Steam wishlist"]["state"] == "ok"
