from app.models.credential import SOURCE_OIDC, STATUS_OK, Credential
from app.security import encrypt_json


def test_settings_requires_auth(client):
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 303


def test_settings_page_shows_not_configured_by_default(authed_client):
    resp = authed_client.get("/settings")
    assert resp.status_code == 200
    assert "not configured" in resp.text.lower()


def _enable_sso_only(db):
    db.add(
        Credential(
            source=SOURCE_OIDC,
            encrypted_payload=encrypt_json(
                {
                    "issuer": "https://auth.example.com",
                    "client_id": "cid",
                    "client_secret": "secret",
                    "enabled": True,
                    "disable_password": True,
                }
            ),
            status=STATUS_OK,
        )
    )
    db.commit()


def test_settings_account_card_shows_password_fields_by_default(authed_client):
    resp = authed_client.get("/settings")
    assert 'name="current_password"' in resp.text


def test_settings_account_card_hides_password_fields_when_sso_only(authed_client, db):
    _enable_sso_only(db)
    resp = authed_client.get("/settings")
    assert 'name="current_password"' not in resp.text
    assert "SSO-only mode" in resp.text


def test_save_account_updates_email(authed_client, db):
    from app.accounts import get_or_create_account_settings

    resp = authed_client.post("/settings/account", data={"email": "person@example.com"})
    assert resp.status_code == 200
    assert get_or_create_account_settings(db).email == "person@example.com"
    assert 'value="person@example.com"' in resp.text


def test_save_account_email_only_does_not_require_current_password(authed_client, db):
    from app.accounts import get_or_create_account_settings

    resp = authed_client.post("/settings/account", data={"email": "person@example.com"})
    assert "incorrect" not in resp.text.lower()
    assert get_or_create_account_settings(db).email == "person@example.com"


def test_save_account_changes_password_with_correct_current_password(authed_client, db):
    from app.security import check_app_password

    resp = authed_client.post(
        "/settings/account",
        data={"current_password": "test-password", "new_password": "brand-new-password", "confirm_password": "brand-new-password"},
    )
    assert resp.status_code == 200
    assert check_app_password("brand-new-password", db)
    assert not check_app_password("test-password", db)


def test_save_account_rejects_wrong_current_password(authed_client, db):
    from app.security import check_app_password

    resp = authed_client.post(
        "/settings/account",
        data={"current_password": "wrong-password", "new_password": "brand-new-password", "confirm_password": "brand-new-password"},
    )
    assert "incorrect" in resp.text.lower()
    assert check_app_password("test-password", db)  # unchanged


def test_save_account_rejects_mismatched_new_passwords(authed_client, db):
    from app.security import check_app_password

    resp = authed_client.post(
        "/settings/account",
        data={"current_password": "test-password", "new_password": "one-password", "confirm_password": "different-password"},
    )
    assert "do not match" in resp.text.lower()
    assert check_app_password("test-password", db)  # unchanged


def test_save_account_password_change_ignored_when_sso_only(authed_client, db):
    from app.security import check_app_password

    _enable_sso_only(db)
    authed_client.post(
        "/settings/account",
        data={"current_password": "test-password", "new_password": "brand-new-password", "confirm_password": "brand-new-password"},
    )
    # Defensive server-side re-check (matches save_oidc's own convention) —
    # a request forged with password fields must not change anything while
    # SSO-only mode is active, even though the fields aren't rendered.
    assert check_app_password("test-password", db)


def test_settings_page_links_to_the_api_docs(authed_client):
    resp = authed_client.get("/settings")
    assert 'href="/docs"' in resp.text
    assert 'href="/redoc"' in resp.text


def test_docs_and_redoc_require_auth_like_everything_else(client):
    # FastAPI's auto-generated docs aren't in deps.py's public-path allowlist,
    # so they get the same session-cookie gate as the rest of the app.
    assert client.get("/docs", follow_redirects=False).status_code == 303
    assert client.get("/redoc", follow_redirects=False).status_code == 303
