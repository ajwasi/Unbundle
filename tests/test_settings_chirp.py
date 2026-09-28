from unittest.mock import AsyncMock, patch

from app.connectors.types import CredentialStatus
from app.models.credential import SOURCE_CHIRP, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json


def test_settings_page_shows_chirp_card(authed_client):
    resp = authed_client.get("/settings")
    assert "Chirp" in resp.text
    assert 'name="email"' in resp.text


def test_save_chirp_success_stores_credentials(authed_client, db):
    with patch(
        "app.routers.settings.chirp_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected — 78 audiobook(s) found in your library.")),
    ):
        resp = authed_client.post("/settings/chirp", data={"email": "a@b.com", "password": "hunter2"})

    assert resp.status_code == 200
    assert "78 audiobook" in resp.text
    cred = db.query(Credential).filter(Credential.source == SOURCE_CHIRP).one()
    assert cred.status == STATUS_OK
    payload = decrypt_json(cred.encrypted_payload)
    assert payload["email"] == "a@b.com"
    assert payload["password"] == "hunter2"


def test_save_chirp_missing_fields_rejected(authed_client, db):
    resp = authed_client.post("/settings/chirp", data={"email": "", "password": ""})
    assert "required" in resp.text.lower()
    cred = db.query(Credential).filter(Credential.source == SOURCE_CHIRP).one()
    assert cred.status == STATUS_ERROR


def test_save_chirp_rejected_login_shown(authed_client, db):
    with patch(
        "app.routers.settings.chirp_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=False, message="Chirp rejected that email/password.")),
    ):
        resp = authed_client.post("/settings/chirp", data={"email": "a@b.com", "password": "wrong"})
    assert "rejected" in resp.text.lower()
    cred = db.query(Credential).filter(Credential.source == SOURCE_CHIRP).one()
    assert cred.status == STATUS_ERROR


def test_settings_page_hides_disconnect_button_when_not_configured(authed_client):
    resp = authed_client.get("/settings")
    assert "/settings/chirp/disconnect" not in resp.text


def test_disconnect_chirp_removes_credential(authed_client, db):
    db.add(Credential(source=SOURCE_CHIRP, status=STATUS_OK, encrypted_payload=None))
    db.commit()

    resp = authed_client.post("/settings/chirp/disconnect")
    assert resp.status_code == 200
    assert db.query(Credential).filter(Credential.source == SOURCE_CHIRP).one_or_none() is None
    assert "not configured" in resp.text.lower()


def test_disconnect_chirp_shows_button_only_when_configured(authed_client, db):
    db.add(Credential(source=SOURCE_CHIRP, status=STATUS_OK))
    db.commit()
    resp = authed_client.get("/settings")
    assert "/settings/chirp/disconnect" in resp.text


def test_disconnect_chirp_is_a_noop_when_nothing_configured(authed_client, db):
    resp = authed_client.post("/settings/chirp/disconnect")
    assert resp.status_code == 200


def test_save_chirp_blank_password_keeps_existing(authed_client, db):
    with patch(
        "app.routers.settings.chirp_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="ok")),
    ):
        authed_client.post("/settings/chirp", data={"email": "a@b.com", "password": "original-password"})
        resp = authed_client.post("/settings/chirp", data={"email": "a@b.com", "password": ""})

    assert resp.status_code == 200
    payload = decrypt_json(db.query(Credential).filter(Credential.source == SOURCE_CHIRP).one().encrypted_payload)
    assert payload["password"] == "original-password"


def test_save_chirp_blank_email_keeps_existing(authed_client, db):
    with patch(
        "app.routers.settings.chirp_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="ok")),
    ):
        authed_client.post("/settings/chirp", data={"email": "original@example.com", "password": "hunter2"})
        resp = authed_client.post("/settings/chirp", data={"email": "", "password": "hunter2"})

    assert resp.status_code == 200
    payload = decrypt_json(db.query(Credential).filter(Credential.source == SOURCE_CHIRP).one().encrypted_payload)
    assert payload["email"] == "original@example.com"
