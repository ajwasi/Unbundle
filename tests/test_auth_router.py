from unittest.mock import AsyncMock, patch

from app.models.credential import SOURCE_APP_AUTH, SOURCE_OIDC, STATUS_OK, Credential
from app.security import check_app_password, encrypt_json


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


def test_login_session_cookie_not_secure_by_default(client):
    resp = client.post("/login", data={"password": "test-password", "next": "/"}, follow_redirects=False)
    assert "secure" not in resp.headers["set-cookie"].lower()


def test_login_session_cookie_secure_when_behind_https_proxy(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.behind_https_proxy", True)
    resp = client.post("/login", data={"password": "test-password", "next": "/"}, follow_redirects=False)
    assert "secure" in resp.headers["set-cookie"].lower()


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


def test_login_rate_limited_after_too_many_attempts(client):
    for _ in range(10):
        resp = client.post("/login", data={"password": "wrong", "next": "/"})
        assert resp.status_code == 401
    resp = client.post("/login", data={"password": "test-password", "next": "/"}, follow_redirects=False)
    assert resp.status_code == 429


def test_logout_clears_session_cookie(authed_client):
    resp = authed_client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # Subsequent protected request should bounce back to login.
    resp2 = authed_client.get("/bundles", follow_redirects=False)
    assert resp2.status_code == 303


def test_user_menu_shows_logout_link_when_authed(authed_client):
    resp = authed_client.get("/")
    assert 'action="/logout"' in resp.text


def test_login_page_has_no_logout_link(client):
    resp = client.get("/login")
    assert 'action="/logout"' not in resp.text


def test_user_menu_not_shown_on_login_page(client):
    resp = client.get("/login")
    assert 'class="user-menu"' not in resp.text


def test_user_menu_shows_fallback_label_when_no_identity_set(authed_client):
    resp = authed_client.get("/")
    assert ">Account<" in resp.text


def test_user_menu_shows_account_settings_email_when_set(authed_client, db):
    from app.models.account_settings import AccountSettings

    db.add(AccountSettings(id=1, email="admin@example.com"))
    db.commit()

    resp = authed_client.get("/")
    assert "admin@example.com" in resp.text


def test_user_menu_links_to_account_settings_card(authed_client):
    resp = authed_client.get("/")
    assert 'href="/settings#account-card"' in resp.text


def test_theme_toggle_button_present_on_both_login_and_authed_pages(authed_client, client):
    # Unlike logout, the theme toggle isn't auth-gated — someone looking at the
    # login screen itself should still be able to switch to dark mode.
    assert 'id="theme-toggle"' in client.get("/login").text
    assert 'id="theme-toggle"' in authed_client.get("/").text


def test_unconfigured_app_redirects_any_request_to_setup(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.get("/bundles", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


def test_setup_page_shows_form_when_not_configured(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.get("/setup")
    assert resp.status_code == 200
    assert 'name="password"' in resp.text
    assert 'name="confirm_password"' in resp.text


def test_setup_page_redirects_to_root_when_already_configured(client):
    resp = client.get("/setup", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_setup_post_redirects_to_root_when_already_configured(client, db):
    resp = client.post(
        "/setup", data={"password": "new-pw", "confirm_password": "new-pw"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    # Never touched — already-configured means this must not act like a reset.
    assert db.query(Credential).filter(Credential.source == SOURCE_APP_AUTH).one_or_none() is None


def test_setup_success_sets_password_and_logs_in(client, db, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.post(
        "/setup", data={"password": "fresh-password", "confirm_password": "fresh-password"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "humble_tracker_session" in resp.cookies
    assert check_app_password("fresh-password", db) is True


def test_setup_with_username_saves_it_and_shows_it_in_the_user_menu(client, db, monkeypatch):
    from app.accounts import get_or_create_account_settings

    monkeypatch.setattr("app.config.settings.app_password", "")
    client.post(
        "/setup",
        data={"username": "admin@example.com", "password": "fresh-password", "confirm_password": "fresh-password"},
        follow_redirects=False,
    )
    assert get_or_create_account_settings(db).email == "admin@example.com"
    resp = client.get("/")
    assert "admin@example.com" in resp.text


def test_setup_without_username_leaves_it_unset(client, db, monkeypatch):
    from app.accounts import get_or_create_account_settings

    monkeypatch.setattr("app.config.settings.app_password", "")
    client.post(
        "/setup", data={"password": "fresh-password", "confirm_password": "fresh-password"}, follow_redirects=False
    )
    assert get_or_create_account_settings(db).email is None
    resp = client.get("/")
    assert ">Account<" in resp.text  # fallback label, no username set


def test_setup_preserves_typed_username_on_validation_error(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.post(
        "/setup",
        data={"username": "admin@example.com", "password": "one-password", "confirm_password": "different-password"},
    )
    assert resp.status_code == 400
    assert 'value="admin@example.com"' in resp.text


def test_setup_rejects_empty_password(client, db, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.post("/setup", data={"password": "", "confirm_password": ""})
    assert resp.status_code == 400
    assert "cannot be empty" in resp.text
    assert db.query(Credential).filter(Credential.source == SOURCE_APP_AUTH).one_or_none() is None


def test_setup_rejects_mismatched_passwords(client, db, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    resp = client.post("/setup", data={"password": "one-password", "confirm_password": "different-password"})
    assert resp.status_code == 400
    assert "do not match" in resp.text
    assert db.query(Credential).filter(Credential.source == SOURCE_APP_AUTH).one_or_none() is None


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


def test_oidc_callback_stores_identity_with_only_sub_when_no_email_claim(client, db):
    from app.security import decode_session_token

    _save_oidc(db, enabled=True)
    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_access_token = AsyncMock(return_value={"userinfo": {"sub": "user1"}})
        resp = client.get("/auth/oidc/callback?code=abc&state=xyz", follow_redirects=False)
    payload = decode_session_token(resp.cookies["humble_tracker_session"])
    assert payload["identity"] == {"sub": "user1"}


def test_oidc_callback_stores_identity_with_email_in_session(client, db):
    from app.security import decode_session_token

    _save_oidc(db, enabled=True)
    userinfo = {"sub": "user1", "email": "person@example.com", "name": "Person Example"}
    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_access_token = AsyncMock(return_value={"userinfo": userinfo})
        resp = client.get("/auth/oidc/callback?code=abc&state=xyz", follow_redirects=False)
    payload = decode_session_token(resp.cookies["humble_tracker_session"])
    assert payload["identity"]["email"] == "person@example.com"
    assert payload["identity"]["name"] == "Person Example"


def test_user_menu_shows_oidc_email_after_sso_login(client, db):
    _save_oidc(db, enabled=True)
    userinfo = {"sub": "user1", "email": "person@example.com"}
    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_access_token = AsyncMock(return_value={"userinfo": userinfo})
        client.get("/auth/oidc/callback?code=abc&state=xyz", follow_redirects=False)

    resp = client.get("/")
    assert "person@example.com" in resp.text


def test_user_menu_prefers_session_identity_over_account_settings_email(client, db):
    from app.models.account_settings import AccountSettings

    db.add(AccountSettings(id=1, email="admin@example.com"))
    db.commit()
    _save_oidc(db, enabled=True)
    userinfo = {"sub": "user1", "email": "sso-person@example.com"}
    with patch("app.routers.auth.build_oauth_client") as mock_build:
        mock_build.return_value.authorize_access_token = AsyncMock(return_value={"userinfo": userinfo})
        client.get("/auth/oidc/callback?code=abc&state=xyz", follow_redirects=False)

    resp = client.get("/")
    assert "sso-person@example.com" in resp.text
    assert "admin@example.com" not in resp.text


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
