import time
from unittest.mock import patch

import pytest

from app.connectors import audible_connector
from app.models.credential import SOURCE_AUDIBLE, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json

_POLL_TIMEOUT_SECONDS = 2.0
_FAKE_LOGIN_URL = "https://amazon.com/ap/signin?fake=1"
_FAKE_REDIRECT_URL = "https://amazon.com/ap/maplanding?openid.oa2.authorization_code=abc"


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
    assert 'name="locale"' in resp.text


def test_settings_page_renders_the_login_dialog(authed_client):
    # The login flow lives in a floating <dialog>, opened from a plain
    # button in the card rather than replacing the card's own content inline.
    resp = authed_client.get("/settings")
    assert '<dialog id="audible-login-modal">' in resp.text
    assert 'id="audible-modal-content"' in resp.text
    assert "showModal()" in resp.text


def test_audible_actions_oob_update_the_card_behind_the_modal(authed_client, db):
    # Every state-changing Audible route's response carries an out-of-band
    # fragment for the card (status badge, Connect/Disconnect) alongside the
    # modal's own content, so the two never go out of sync.
    with patch("audible.Authenticator.from_login_external", return_value="fake-auth"):
        resp = authed_client.post("/settings/audible/login", data={"locale": "us"})
        assert _wait_until(lambda: audible_connector.login_result() is not None)
    assert 'id="audible-card-body" hx-swap-oob="true"' in resp.text


def test_start_login_shows_login_url_prompt(authed_client, db):
    def fake_from_login_external(locale, login_url_callback=None, **kwargs):
        answer = login_url_callback(_FAKE_LOGIN_URL)
        return "fake-authenticator" if answer else None

    with patch("audible.Authenticator.from_login_external", side_effect=fake_from_login_external):
        resp = authed_client.post("/settings/audible/login", data={"locale": "us"})
        assert resp.status_code == 200
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        assert audible_connector.login_status().login_url == _FAKE_LOGIN_URL

        # Poll route reflects the pending prompt without resolving it.
        status_resp = authed_client.get("/settings/audible/login/status")
        assert _FAKE_LOGIN_URL in status_resp.text

        answer_resp = authed_client.post("/settings/audible/login/answer", data={"pasted_url": _FAKE_REDIRECT_URL})
        assert answer_resp.status_code == 200

        assert _wait_until(lambda: audible_connector.login_result() is not None)


def test_completed_login_shows_confirmation_in_the_modal(authed_client, db):
    with patch("audible.Authenticator.from_login_external") as mock_from_login:
        mock_from_login.return_value.to_dict.return_value = {"access_token": "AT", "locale_code": "us"}
        mock_from_login.return_value.customer_info = None  # real Authenticator default — see settings.py's own comment
        authed_client.post("/settings/audible/login", data={"locale": "us"})
        assert _wait_until(lambda: audible_connector.login_result() is not None)

        resp = authed_client.get("/settings/audible/login/status")
    assert "Connected to Audible." in resp.text


def test_completed_login_shows_connected_as_name_when_available(authed_client, db):
    with patch("audible.Authenticator.from_login_external") as mock_from_login:
        mock_from_login.return_value.to_dict.return_value = {"access_token": "AT", "locale_code": "us"}
        mock_from_login.return_value.customer_info = {"name": "Jane Reader", "user_id": "AXXXXXXXXX"}
        authed_client.post("/settings/audible/login", data={"locale": "us"})
        assert _wait_until(lambda: audible_connector.login_result() is not None)

        resp = authed_client.get("/settings/audible/login/status")
    assert "Connected to Audible as Jane Reader." in resp.text


def test_completed_login_saves_credential_and_clears_pending(authed_client, db):
    with patch("audible.Authenticator.from_login_external") as mock_from_login:
        fake_auth = mock_from_login.return_value
        fake_auth.to_dict.return_value = {"access_token": "AT", "locale_code": "us"}
        fake_auth.customer_info = None

        authed_client.post("/settings/audible/login", data={"locale": "us"})
        assert _wait_until(lambda: audible_connector.login_result() is not None)

        resp = authed_client.get("/settings/audible/login/status")
        assert resp.status_code == 200

    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one()
    assert cred.status == STATUS_OK
    assert decrypt_json(cred.encrypted_payload)["access_token"] == "AT"
    assert audible_connector.login_status() is None


def test_failed_login_records_error_and_clears_pending(authed_client, db):
    with patch("audible.Authenticator.from_login_external", side_effect=ValueError("bad redirect url")):
        authed_client.post("/settings/audible/login", data={"locale": "us"})
        assert _wait_until(lambda: audible_connector.login_result() is not None)

        resp = authed_client.get("/settings/audible/login/status")
        assert "bad redirect url" in resp.text

    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one()
    assert cred.status == STATUS_ERROR
    assert cred.last_error == "bad redirect url"


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
