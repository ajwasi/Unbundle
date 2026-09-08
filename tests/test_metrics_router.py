import re

from tests.factories import make_order


def _metric_value(text: str, name: str, labels: str = "") -> float | None:
    """Pulls one Prometheus exposition line's value out of raw /metrics text.
    `labels`, if given, must appear verbatim within that line's {...} block —
    good enough for these tests without a full exposition-format parser.
    """
    for line in text.splitlines():
        if line.startswith(name) and (not labels or labels in line):
            return float(line.rsplit(" ", 1)[1])
    return None


def test_metrics_endpoint_reachable_without_login(client):
    resp = client.get("/metrics", follow_redirects=False)
    assert resp.status_code == 200


def test_metrics_endpoint_returns_prometheus_format(client):
    resp = client.get("/metrics")
    assert "text/plain" in resp.headers["content-type"]
    # Always present regardless of app state — prometheus_client's own
    # auto-registered process collectors, confirmed live in a real prototype run.
    assert "python_gc_objects_collected_total" in resp.text


def test_metrics_endpoint_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.metrics_token", "secret-token")
    resp = client.get("/metrics")
    assert resp.status_code == 401


def test_metrics_endpoint_accepts_correct_token(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.metrics_token", "secret-token")
    resp = client.get("/metrics", headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 200


def test_metrics_endpoint_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.metrics_token", "secret-token")
    resp = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_bundle_count_gauge_reflects_real_db_state(client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order())
    make_bundle(gamekey="GK2", order=make_order())
    resp = client.get("/metrics")
    assert _metric_value(resp.text, "humble_tracker_bundles_count") == 2.0


def test_bundle_count_gauge_is_zero_with_no_bundles(client):
    resp = client.get("/metrics")
    assert _metric_value(resp.text, "humble_tracker_bundles_count") == 0.0


def test_connector_status_gauge_reflects_credential_state(client, db):
    from app.models.credential import SOURCE_STEAM, STATUS_OK, Credential

    db.add(Credential(source=SOURCE_STEAM, status=STATUS_OK))
    db.commit()

    resp = client.get("/metrics")
    assert _metric_value(resp.text, "humble_tracker_connector_status", 'source="steam"') == 1.0
    assert _metric_value(resp.text, "humble_tracker_connector_status", 'source="humble"') == 0.0


def test_unredeemed_key_gauge_counts_only_unredeemed_steam_keys(client, make_bundle, db):
    from app.models.bundle_entitlement import BundleEntitlement

    bundle = make_bundle(gamekey="GK1", order=make_order())
    db.add(BundleEntitlement(gamekey=bundle.gamekey, machine_name="a", keyindex=0, key_name="A", steam_app_id="1", steam_owned=False))
    db.add(BundleEntitlement(gamekey=bundle.gamekey, machine_name="b", keyindex=0, key_name="B", steam_app_id="2", steam_owned=True))
    db.commit()

    resp = client.get("/metrics")
    assert _metric_value(resp.text, "humble_tracker_entitlements_unredeemed", 'platform="steam"') == 1.0


def test_unredeemed_key_gauge_counts_name_matched_gog_keys_too(client, make_bundle, db):
    # gog_owned can be set via name-matching alone (sync/gog_sync.py), with no
    # gog_id ever populated — this gauge must not require gog_id, or every
    # name-matched row would be silently excluded (same bug already fixed in
    # routers/gog.py's own unredeemed-list query).
    from app.models.bundle_entitlement import BundleEntitlement

    bundle = make_bundle(gamekey="GK1", order=make_order())
    db.add(BundleEntitlement(gamekey=bundle.gamekey, machine_name="a", keyindex=0, key_name="Liberated", gog_id=None, gog_owned=False))
    db.commit()

    resp = client.get("/metrics")
    assert _metric_value(resp.text, "humble_tracker_entitlements_unredeemed", 'platform="gog"') == 1.0


def test_update_available_gauge_reflects_cached_flag(client, monkeypatch):
    monkeypatch.setattr("app.version._update_available", True)
    resp = client.get("/metrics")
    assert _metric_value(resp.text, "humble_tracker_update_available") == 1.0

    monkeypatch.setattr("app.version._update_available", False)
    resp = client.get("/metrics")
    assert _metric_value(resp.text, "humble_tracker_update_available") == 0.0


def test_rate_limit_rejection_counter_increments_on_429(client):
    # rate_limit() runs before require_csrf() in /login's dependency list, so this
    # counts every attempt regardless of CSRF validity — no need for a real token here.
    before = _metric_value(client.get("/metrics").text, "humble_tracker_rate_limit_rejections_total", 'limiter="login"') or 0.0

    for _ in range(11):
        client.post("/login", data={"password": "wrong"})

    after = _metric_value(client.get("/metrics").text, "humble_tracker_rate_limit_rejections_total", 'limiter="login"')
    assert after is not None
    assert after >= before + 1
