import json

import pytest
from cryptography.fernet import Fernet

from app.security import (
    _derive_key,
    _derive_key_legacy,
    check_app_password,
    create_session_token,
    decode_session_token,
    decrypt_json,
    encrypt_json,
    hash_password,
    verify_password_hash,
    verify_session_token,
)


def test_encrypt_decrypt_round_trip():
    data = {"session_key": "abc123"}
    token = encrypt_json(data)
    assert token != data
    assert decrypt_json(token) == data


def test_new_kdf_is_not_just_the_legacy_kdf_relabeled():
    assert _derive_key("same-secret") != _derive_key_legacy("same-secret")


def test_decrypt_json_still_opens_a_pre_upgrade_legacy_format_token():
    # Simulates a Credential.encrypted_payload written before the PBKDF2 upgrade —
    # decrypt_json must still open it under the currently-configured APP_SECRET_KEY
    # (conftest.py sets this to "test-only-secret-key") via its fallback path, so
    # existing stored credentials don't go dark the moment this code ships.
    legacy_fernet = Fernet(_derive_key_legacy("test-only-secret-key"))
    token = legacy_fernet.encrypt(json.dumps({"legacy": True}).encode("utf-8"))
    assert decrypt_json(token) == {"legacy": True}


def test_encrypt_json_can_target_a_key_other_than_the_configured_one():
    # This is what the rotate-secret-key CLI tool relies on: encrypting under a
    # *new* key before that key is the one actually configured in settings.
    token = encrypt_json({"a": 1}, secret_key="a-different-key")
    with pytest.raises(ValueError):
        decrypt_json(token)  # wrong under the still-configured settings.app_secret_key
    assert decrypt_json(token, secret_key="a-different-key") == {"a": 1}


def test_decrypt_rejects_tampered_token():
    token = encrypt_json({"a": 1})
    tampered = token[:-1] + (b"0" if token[-1:] != b"0" else b"1")
    with pytest.raises(ValueError):
        decrypt_json(tampered)


def test_session_token_round_trip():
    token = create_session_token()
    assert verify_session_token(token)


def test_session_token_rejects_garbage():
    assert not verify_session_token("not-a-real-token")
    assert not verify_session_token(None)


def test_create_session_token_without_identity_omits_identity_key():
    token = create_session_token()
    assert "identity" not in decode_session_token(token)


def test_create_session_token_embeds_identity():
    token = create_session_token(identity={"email": "person@example.com", "sub": "abc"})
    payload = decode_session_token(token)
    assert payload["identity"] == {"email": "person@example.com", "sub": "abc"}
    assert payload["authenticated"] is True


def test_decode_session_token_rejects_garbage():
    assert decode_session_token("not-a-real-token") is None
    assert decode_session_token(None) is None


def test_hash_password_round_trip():
    stored = hash_password("correct horse battery staple")
    assert verify_password_hash("correct horse battery staple", stored)


def test_hash_password_rejects_wrong_password():
    stored = hash_password("correct horse battery staple")
    assert not verify_password_hash("wrong password", stored)


def test_hash_password_uses_a_random_salt():
    # Same password, hashed twice, must not produce identical output — a fixed
    # salt would make the stored hash vulnerable to a precomputed rainbow table.
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second


def test_verify_password_hash_rejects_malformed_stored_value():
    assert not verify_password_hash("anything", "not-a-valid-stored-hash")


def test_check_app_password_falls_back_to_env_var_when_no_db_override(db):
    # conftest.py sets APP_PASSWORD=test-password for the whole suite.
    assert check_app_password("test-password", db)
    assert not check_app_password("wrong-password", db)


def test_check_app_password_db_override_takes_precedence(db):
    from app.models.credential import SOURCE_APP_AUTH, STATUS_OK, Credential

    db.add(Credential(source=SOURCE_APP_AUTH, encrypted_payload=encrypt_json({"password_hash": hash_password("new-password")}), status=STATUS_OK))
    db.commit()

    assert check_app_password("new-password", db)
    # The original env-var password must stop working once a DB override exists —
    # this is the whole point of the emergency `set-password` CLI recovery tool.
    assert not check_app_password("test-password", db)


def test_every_encryption_gets_its_own_random_salt():
    first = encrypt_json({"a": 1})
    second = encrypt_json({"a": 1})
    assert first.startswith(b"v2:") and second.startswith(b"v2:")
    assert first.split(b":", 2)[1] != second.split(b":", 2)[1]


def test_decrypt_json_still_opens_a_v1_fixed_salt_token():
    # Written by every release before per-credential salts.
    v1_token = Fernet(_derive_key("test-only-secret-key")).encrypt(json.dumps({"v1": True}).encode("utf-8"))
    assert decrypt_json(v1_token) == {"v1": True}


def test_v2_token_with_tampered_salt_is_rejected():
    token = encrypt_json({"a": 1})
    prefix, salt, rest = token.split(b":", 2)
    tampered = b":".join([prefix, b"A" * len(salt), rest])
    with pytest.raises(ValueError):
        decrypt_json(tampered)


def test_malformed_v2_token_is_rejected():
    with pytest.raises(ValueError):
        decrypt_json(b"v2:not-enough-parts")
