import time
from unittest.mock import patch

import pytest

from app.connectors import audible_connector
from app.models.credential import SOURCE_AUDIBLE, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json

_POLL_TIMEOUT_SECONDS = 2.0


@pytest.fixture(autouse=True)
def _reset_pending():
    audible_connector.clear_pending()
    yield
    audible_connector.clear_pending()


def _wait_until(predicate, timeout=_POLL_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_settings_page_shows_audible_card(authed_client):
    resp = authed_client.get("/settings")
    assert "Audible" in resp.text
    assert 'name="username"' in resp.text
    assert 'name="password"' in resp.text


def test_start_login_rejects_blank_credentials(authed_client, db):
    resp = authed_client.post("/settings/audible/login", data={"username": "", "password": ""})
    assert "required" in resp.text.lower()
    assert audible_connector.login_status() is None


def test_start_login_shows_captcha_prompt(authed_client, db):
    def fake_from_login(username, password, locale, captcha_callback=None, otp_callback=None, **kwargs):
        answer = captcha_callback("https://example.com/captcha.jpg")
        return "fake-auth" if answer else None

    with patch("audible.Authenticator.from_login", side_effect=fake_from_login):
        resp = authed_client.post(
            "/settings/audible/login", data={"username": "me@example.com", "password": "hunter2", "locale": "us"}
        )
        assert resp.status_code == 200
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        assert audible_connector.login_status().kind == "captcha"

        # Poll route reflects the pending prompt without resolving it.
        status_resp = authed_client.get("/settings/audible/login/status")
        assert "CAPTCHA" in status_resp.text

        answer_resp = authed_client.post("/settings/audible/login/answer", data={"answer": "solved"})
        assert answer_resp.status_code == 200

        assert _wait_until(lambda: audible_connector.login_result() is not None)


def test_completed_login_saves_credential_and_clears_pending(authed_client, db):
    with patch("audible.Authenticator.from_login") as mock_from_login:
        fake_auth = mock_from_login.return_value
        fake_auth.to_dict.return_value = {"access_token": "AT", "locale_code": "us"}

        authed_client.post("/settings/audible/login", data={"username": "me@example.com", "password": "hunter2"})
        assert _wait_until(lambda: audible_connector.login_result() is not None)

        resp = authed_client.get("/settings/audible/login/status")
        assert resp.status_code == 200

    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one()
    assert cred.status == STATUS_OK
    assert decrypt_json(cred.encrypted_payload)["access_token"] == "AT"
    assert audible_connector.login_status() is None


def test_failed_login_records_error_and_clears_pending(authed_client, db):
    with patch("audible.Authenticator.from_login", side_effect=ValueError("bad credentials")):
        authed_client.post("/settings/audible/login", data={"username": "me@example.com", "password": "wrong"})
        assert _wait_until(lambda: audible_connector.login_result() is not None)

        resp = authed_client.get("/settings/audible/login/status")
        assert "bad credentials" in resp.text

    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one()
    assert cred.status == STATUS_ERROR
    assert cred.last_error == "bad credentials"


def test_disconnect_audible_removes_credential(authed_client, db):
    db.add(Credential(source=SOURCE_AUDIBLE, status=STATUS_OK))
    db.commit()

    resp = authed_client.post("/settings/audible/disconnect")
    assert resp.status_code == 200
    assert db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one_or_none() is None


def test_disconnect_button_shown_only_when_configured(authed_client, db):
    resp = authed_client.get("/settings")
    assert "/settings/audible/disconnect" not in resp.text

    db.add(Credential(source=SOURCE_AUDIBLE, status=STATUS_OK))
    db.commit()
    resp = authed_client.get("/settings")
    assert "/settings/audible/disconnect" in resp.text
