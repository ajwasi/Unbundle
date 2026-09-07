import pytest

from app.security import (
    check_app_password,
    create_session_token,
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


def test_hash_password_round_trip():
    stored = hash_password("correct horse battery staple")
    assert verify_password_hash("correct horse battery staple", stored)


def test_hash_password_rejects_wrong_password():
    stored = hash_password("correct horse battery staple")
    assert not verify_password_hash("wrong password", stored)


def test_hash_password_uses_a_random_salt():
    # Same password, hashed twice, must not produce identical output — a fixed
    # salt would make the stored hash vulnerable to a precomputed rainbow table.
    assert hash_password("same-password") != hash_password("same-password")


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
