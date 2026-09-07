from app.cli import _disable_oidc, _set_password
from app.models.credential import SOURCE_APP_AUTH, SOURCE_OIDC, STATUS_NOT_CONFIGURED, Credential
from app.security import check_app_password, decrypt_json, encrypt_json


def test_disable_oidc_returns_false_when_not_configured(db):
    assert _disable_oidc() is False


def test_disable_oidc_turns_off_enabled_and_disable_password(db):
    db.add(
        Credential(
            source=SOURCE_OIDC,
            encrypted_payload=encrypt_json({"issuer": "x", "client_id": "y", "client_secret": "z", "enabled": True, "disable_password": True}),
        )
    )
    db.commit()

    assert _disable_oidc() is True

    cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one()
    payload = decrypt_json(cred.encrypted_payload)
    assert payload["enabled"] is False
    assert payload["disable_password"] is False
    assert cred.status == STATUS_NOT_CONFIGURED


def test_disable_oidc_preserves_issuer_and_credentials_for_later_re_enable(db):
    db.add(
        Credential(
            source=SOURCE_OIDC,
            encrypted_payload=encrypt_json(
                {"issuer": "https://auth.example.com", "client_id": "keep-me", "client_secret": "keep-me-too", "enabled": True, "disable_password": True}
            ),
        )
    )
    db.commit()

    _disable_oidc()

    payload = decrypt_json(db.query(Credential).filter(Credential.source == SOURCE_OIDC).one().encrypted_payload)
    assert payload["issuer"] == "https://auth.example.com"
    assert payload["client_id"] == "keep-me"
    assert payload["client_secret"] == "keep-me-too"


def test_set_password_creates_new_credential_row(db):
    assert db.query(Credential).filter(Credential.source == SOURCE_APP_AUTH).one_or_none() is None
    _set_password("brand-new-password")
    cred = db.query(Credential).filter(Credential.source == SOURCE_APP_AUTH).one()
    assert cred is not None


def test_set_password_new_password_authenticates(db):
    _set_password("brand-new-password")
    assert check_app_password("brand-new-password", db)


def test_set_password_overrides_previous_db_password(db):
    _set_password("first-password")
    _set_password("second-password")
    assert check_app_password("second-password", db)
    assert not check_app_password("first-password", db)


def test_set_password_supersedes_env_var_password(db):
    # conftest.py sets APP_PASSWORD=test-password for the whole suite.
    assert check_app_password("test-password", db)
    _set_password("overridden-password")
    assert not check_app_password("test-password", db)
    assert check_app_password("overridden-password", db)
