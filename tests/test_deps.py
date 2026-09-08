from unittest.mock import AsyncMock, patch

from app.models.credential import SOURCE_OIDC, STATUS_OK, Credential
from app.security import encrypt_json


def _save_oidc(db, **overrides):
    payload = {"issuer": "https://auth.example.com", "client_id": "cid", "client_secret": "s", "enabled": True, "disable_password": False}
    payload.update(overrides)
    db.add(Credential(source=SOURCE_OIDC, encrypted_payload=encrypt_json(payload), status=STATUS_OK))
    db.commit()


def test_protected_route_redirects_when_unauthenticated(client):
    resp = client.get("/bundles", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_static_files_are_never_gated(client):
    resp = client.get("/static/css/app.css", follow_redirects=False)
    assert resp.status_code != 303


def test_login_page_itself_is_never_gated(client):
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 200


def test_unconfigured_redirects_to_setup_instead_of_running_wide_open(client, monkeypatch):
    # Reversed from this app's original default (explicit user decision): no
    # configured login method at all now forces /setup rather than letting
    # requests through — see routers/auth.py's /setup docstring.
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.get("/bundles", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


def test_setup_itself_is_never_redirected_away_from_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.get("/setup", follow_redirects=False)
    assert resp.status_code == 200


def test_gated_when_only_password_set(client):
    resp = client.get("/bundles", follow_redirects=False)
    assert resp.status_code == 303  # conftest sets APP_PASSWORD, no OIDC configured


def test_gated_when_only_oidc_enabled_and_no_password(client, db, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    _save_oidc(db, enabled=True)
    resp = client.get("/bundles", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_password_session_still_works_when_oidc_also_enabled(authed_client, db):
    _save_oidc(db, enabled=True, disable_password=False)
    resp = authed_client.get("/bundles")
    assert resp.status_code == 200


def test_existing_session_rejected_once_password_disabled_requires_new_login(authed_client, db):
    # An already-issued session cookie is still just a signed "authenticated: true"
    # token, unrelated to which method is currently allowed — so a pre-existing
    # session must keep working; disable_password only blocks *new* password logins.
    _save_oidc(db, enabled=True, disable_password=True)
    resp = authed_client.get("/bundles")
    assert resp.status_code == 200


def test_warning_banner_shown_when_wide_open(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.get("/")
    assert "no login is configured" in resp.text.lower()


def test_warning_banner_absent_when_password_configured(authed_client):
    resp = authed_client.get("/")
    assert "no login is configured" not in resp.text.lower()


def test_warning_banner_shown_on_login_page_when_wide_open(client, monkeypatch):
    # An edge case (AuthMiddleware wouldn't normally send anyone here in this
    # state), but someone could still browse to it directly, and the banner
    # should reflect reality wherever it's checked.
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.get("/login")
    assert "no login is configured" in resp.text.lower()


def test_auth_oidc_paths_are_never_gated(client, db):
    # If AuthMiddleware gated this path, an unauthenticated hit would bounce to
    # /login instead of ever reaching the real handler below — a redirect loop,
    # since the login page itself links here. Mock out the actual IdP round trip
    # (covered separately in test_auth_router.py) to isolate what this test cares
    # about: that the middleware lets the request through at all.
    from fastapi.responses import RedirectResponse

    _save_oidc(db, enabled=True)
    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_redirect = AsyncMock(
            return_value=RedirectResponse("https://auth.example.com/authorize", status_code=302)
        )
        resp = client.get("/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 302
