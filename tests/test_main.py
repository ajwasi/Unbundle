from app.main import _startup_warnings


def test_no_warnings_when_properly_configured(db):
    # conftest.py sets a real (non-default) APP_SECRET_KEY and a real APP_PASSWORD.
    assert _startup_warnings() == []


def test_warns_when_no_auth_configured(db, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    warnings = _startup_warnings()
    assert any("no authentication" in w.lower() for w in warnings)


def test_security_headers_present_on_an_authenticated_page(authed_client):
    resp = authed_client.get("/downloads")
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("content-security-policy") == "frame-ancestors 'none'"
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("referrer-policy") == "same-origin"


def test_security_headers_present_on_the_login_page(client):
    # Must apply regardless of auth state — the login/setup pages are exactly
    # where a clickjacking overlay would be most useful to an attacker.
    resp = client.get("/login")
    assert resp.headers.get("x-frame-options") == "DENY"


def test_security_headers_present_on_a_static_file(client):
    resp = client.get("/static/css/app.css")
    assert resp.headers.get("x-frame-options") == "DENY"


def test_responses_are_gzip_compressed_above_the_size_threshold(authed_client):
    # GZipMiddleware's default 500-byte floor — the full /downloads page (base
    # layout + sidebar + cards) is comfortably over that regardless of seeded
    # data, so no fixtures needed. httpx (TestClient) negotiates and decodes
    # gzip transparently, so the content-encoding header — not resp.text,
    # which looks identical either way — is what actually proves compression
    # happened.
    resp = authed_client.get("/downloads")
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"


def test_update_available_link_points_at_the_tags_page(authed_client, monkeypatch):
    # A fixed link to the tags listing, not a per-version compare URL — simpler
    # and always correct regardless of whether a check has ever run.
    import app.version as version_module

    monkeypatch.setattr(version_module, "_update_available", True)

    resp = authed_client.get("/")
    assert "Update available" in resp.text
    assert 'href="https://github.com/ajwasi/Unbundle/tags"' in resp.text


def test_no_update_link_shown_when_already_current(authed_client, monkeypatch):
    import app.version as version_module

    monkeypatch.setattr(version_module, "_update_available", False)

    resp = authed_client.get("/")
    assert "Update available" not in resp.text


def test_check_for_updates_button_shown_for_a_tagged_build(authed_client, monkeypatch):
    import app.version as version_module

    monkeypatch.setattr(version_module, "get_version", lambda: "v1.0.0")
    resp = authed_client.get("/")
    assert "Check for updates" in resp.text


def test_check_for_updates_button_hidden_for_a_non_tagged_build(authed_client, monkeypatch):
    import app.version as version_module

    monkeypatch.setattr(version_module, "get_version", lambda: "abc1234")
    resp = authed_client.get("/")
    assert "Check for updates" not in resp.text


def test_manual_check_shows_up_to_date_message(authed_client, monkeypatch):
    from datetime import datetime
    from unittest.mock import AsyncMock

    import app.version as version_module

    monkeypatch.setattr(version_module, "get_version", lambda: "v1.0.0")
    monkeypatch.setattr(version_module, "check_for_update", AsyncMock(return_value=True))
    monkeypatch.setattr(version_module, "_update_available", False)
    monkeypatch.setattr(version_module, "_last_checked_at", datetime.utcnow())

    resp = authed_client.post("/version/check")
    assert resp.status_code == 200
    assert "Up to date" in resp.text
    assert "checked" in resp.text


def test_manual_check_shows_failure_message_on_failure(authed_client, monkeypatch):
    import app.version as version_module
    from unittest.mock import AsyncMock

    monkeypatch.setattr(version_module, "get_version", lambda: "v1.0.0")
    monkeypatch.setattr(version_module, "check_for_update", AsyncMock(return_value=False))

    resp = authed_client.post("/version/check")
    assert resp.status_code == 200
    assert "Check failed" in resp.text


def test_manual_check_is_rate_limited(authed_client, monkeypatch):
    import app.version as version_module
    from unittest.mock import AsyncMock

    from app.routers.version import _check_limiter

    monkeypatch.setattr(version_module, "get_version", lambda: "v1.0.0")
    monkeypatch.setattr(version_module, "check_for_update", AsyncMock(return_value=True))
    _check_limiter.reset()

    for _ in range(5):
        assert authed_client.post("/version/check").status_code == 200
    assert authed_client.post("/version/check").status_code == 429


def test_no_auth_warning_when_oidc_enabled_instead(db, monkeypatch):
    from app.models.credential import SOURCE_OIDC, STATUS_OK, Credential
    from app.security import encrypt_json

    monkeypatch.setattr("app.config.settings.app_password", "")
    db.add(
        Credential(
            source=SOURCE_OIDC,
            encrypted_payload=encrypt_json({"issuer": "https://x", "client_id": "c", "client_secret": "s", "enabled": True, "disable_password": False}),
            status=STATUS_OK,
        )
    )
    db.commit()
    assert _startup_warnings() == []
