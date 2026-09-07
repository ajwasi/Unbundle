from unittest.mock import AsyncMock, patch

from app.models.credential import SOURCE_OIDC, STATUS_OK, Credential
from app.security import encrypt_json


def _save_oidc(db, **overrides):
    payload = {"issuer": "https://auth.example.com", "client_id": "cid", "client_secret": "csecret", "enabled": True, "disable_password": False}
    payload.update(overrides)
    db.add(Credential(source=SOURCE_OIDC, encrypted_payload=encrypt_json(payload), status=STATUS_OK))
    db.commit()


def test_login_page_shows_password_field_by_default(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'name="password"' in resp.text
    assert "Sign in with SSO" not in resp.text


def test_login_success_sets_session_cookie_and_redirects(client):
    resp = client.post("/login", data={"password": "test-password", "next": "/"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "humble_tracker_session" in resp.cookies


def test_login_wrong_password_rejected(client):
    resp = client.post("/login", data={"password": "wrong", "next": "/"})
    assert resp.status_code == 401
    assert "Incorrect password" in resp.text


def test_login_redirects_to_requested_next(client):
    resp = client.post("/login", data={"password": "test-password", "next": "/catalog"}, follow_redirects=False)
    assert resp.headers["location"] == "/catalog"


def test_login_rejects_scheme_relative_next_as_open_redirect(client):
    resp = client.post(
        "/login", data={"password": "test-password", "next": "//evil.example.com"}, follow_redirects=False
    )
    assert resp.headers["location"] == "/"


def test_login_rejects_absolute_url_next_as_open_redirect(client):
    resp = client.post(
        "/login",
        data={"password": "test-password", "next": "https://evil.example.com"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/"


def test_login_rejects_backslash_next_as_open_redirect(client):
    resp = client.post(
        "/login", data={"password": "test-password", "next": "/\\evil.example.com"}, follow_redirects=False
    )
    assert resp.headers["location"] == "/"


def test_login_page_sanitizes_next_in_rendered_form(client):
    resp = client.get("/login?next=https://evil.example.com")
    assert 'value="/"' in resp.text
    assert "evil.example.com" not in resp.text


def test_logout_clears_session_cookie(authed_client):
    resp = authed_client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # Subsequent protected request should bounce back to login.
    resp2 = authed_client.get("/bundles", follow_redirects=False)
    assert resp2.status_code == 303


def test_login_page_shows_sso_button_when_oidc_enabled(client, db):
    _save_oidc(db, enabled=True)
    resp = client.get("/login")
    assert "Sign in with SSO" in resp.text
    assert 'name="password"' in resp.text  # still available alongside


def test_login_page_hides_password_when_disabled(client, db):
    _save_oidc(db, enabled=True, disable_password=True)
    resp = client.get("/login")
    assert "Sign in with SSO" in resp.text
    assert 'name="password"' not in resp.text


def test_login_post_rejected_server_side_when_password_disabled(client, db):
    _save_oidc(db, enabled=True, disable_password=True)
    resp = client.post("/login", data={"password": "test-password", "next": "/"})
    assert resp.status_code == 401


def test_oidc_login_404_when_not_configured(client):
    resp = client.get("/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 404


def test_oidc_login_redirects_to_idp_when_configured(client, db):
    from fastapi.responses import RedirectResponse

    _save_oidc(db, enabled=True)
    fake_redirect = AsyncMock(return_value=RedirectResponse("https://auth.example.com/authorize?state=xyz", status_code=302))
    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_redirect = fake_redirect
        resp = client.get("/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://auth.example.com/authorize")
    fake_redirect.assert_awaited_once()


def test_oidc_callback_404_when_not_configured(client):
    resp = client.get("/auth/oidc/callback")
    assert resp.status_code == 404


def test_oidc_callback_success_sets_session_cookie(client, db):
    _save_oidc(db, enabled=True)
    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_access_token = AsyncMock(return_value={"userinfo": {"sub": "user1"}})
        resp = client.get("/auth/oidc/callback?code=abc&state=xyz", follow_redirects=False)
    assert resp.status_code == 303
    assert "humble_tracker_session" in resp.cookies


def test_oidc_callback_sanitizes_malicious_next_from_session(client, db):
    from fastapi.responses import RedirectResponse

    _save_oidc(db, enabled=True)
    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_redirect = AsyncMock(
            return_value=RedirectResponse("https://auth.example.com/authorize", status_code=302)
        )
        client.get("/auth/oidc/login?next=https://evil.example.com", follow_redirects=False)

    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_access_token = AsyncMock(return_value={"userinfo": {"sub": "user1"}})
        resp = client.get("/auth/oidc/callback?code=abc&state=xyz", follow_redirects=False)
    assert resp.headers["location"] == "/"


def test_oidc_callback_failure_shows_login_error(client, db):
    from authlib.integrations.base_client.errors import OAuthError

    _save_oidc(db, enabled=True)
    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_access_token = AsyncMock(side_effect=OAuthError(error="access_denied", description="user cancelled"))
        resp = client.get("/auth/oidc/callback?error=access_denied")
    assert resp.status_code == 401
    assert "SSO login failed" in resp.text
