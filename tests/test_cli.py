import json

import pytest
from cryptography.fernet import Fernet

from app.cli import _disable_oidc, _rotate_secret_key, _set_password
from app.models.credential import SOURCE_APP_AUTH, SOURCE_GOG, SOURCE_OIDC, STATUS_NOT_CONFIGURED, Credential
from app.security import _derive_key, _derive_key_legacy, check_app_password, decrypt_json, encrypt_json


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


def test_rotate_secret_key_returns_zero_when_nothing_stored(db):
    assert _rotate_secret_key("new-key") == 0


def test_rotate_secret_key_migrates_every_row_with_a_payload(db):
    db.add(Credential(source=SOURCE_OIDC, encrypted_payload=encrypt_json({"issuer": "x"})))
    db.add(Credential(source=SOURCE_GOG, encrypted_payload=encrypt_json({"refresh_token": "y"})))
    db.add(Credential(source=SOURCE_APP_AUTH, encrypted_payload=None))  # no payload — must be skipped, not crash
    db.commit()

    count = _rotate_secret_key("brand-new-key")
    assert count == 2


def test_rotate_secret_key_result_is_readable_under_the_new_key_only(db):
    db.add(Credential(source=SOURCE_OIDC, encrypted_payload=encrypt_json({"issuer": "x"})))
    db.commit()

    _rotate_secret_key("brand-new-key")

    cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one()
    # No longer readable under the key still configured in settings (conftest's
    # "test-only-secret-key") — this is the whole point: rotation actually rotates.
    with pytest.raises(ValueError):
        decrypt_json(cred.encrypted_payload)
    new_key_fernet = Fernet(_derive_key("brand-new-key"))
    assert json.loads(new_key_fernet.decrypt(cred.encrypted_payload)) == {"issuer": "x"}


def test_rotate_secret_key_migrates_a_pre_upgrade_legacy_format_row_too(db):
    # Full pipeline: a row written before the PBKDF2 upgrade must still be
    # readable (via decrypt_json's legacy fallback) and successfully migrate to
    # both the new KDF and the new key in one pass.
    legacy_fernet = Fernet(_derive_key_legacy("test-only-secret-key"))
    legacy_token = legacy_fernet.encrypt(json.dumps({"legacy": True}).encode("utf-8"))
    db.add(Credential(source=SOURCE_OIDC, encrypted_payload=legacy_token))
    db.commit()

    count = _rotate_secret_key("brand-new-key")
    assert count == 1

    cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one()
    new_key_fernet = Fernet(_derive_key("brand-new-key"))
    assert json.loads(new_key_fernet.decrypt(cred.encrypted_payload)) == {"legacy": True}
