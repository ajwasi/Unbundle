"""Rules that live in routers/settings/media_server.py's shared factory
(each integration's own happy path is covered in test_settings_<name>.py)."""

from unittest.mock import AsyncMock, patch

from app.connectors.types import CredentialStatus
from app.models.credential import SOURCE_NAVIDROME, SOURCE_PLEX, Credential
from app.security import decrypt_json

_OK = CredentialStatus(ok=True, message="OK")


def _payload(db, source):
    db.expire_all()
    return decrypt_json(Credential.get(db, source).encrypted_payload)


def test_a_blank_library_id_clears_it_while_a_blank_token_keeps_the_saved_one(authed_client, db):
    with patch("app.routers.settings.plex.plex_connector.check_credentials", new=AsyncMock(return_value=_OK)):
        authed_client.post("/settings/plex", data={"base_url": "http://p:32400", "token": "tok", "library_id": "3"})
        authed_client.post("/settings/plex", data={"base_url": "", "token": "", "library_id": ""})

    assert _payload(db, SOURCE_PLEX) == {"base_url": "http://p:32400", "token": "tok", "library_id": ""}


def test_navidrome_password_is_stored_exactly_as_typed_but_other_fields_are_stripped(authed_client, db):
    with patch("app.routers.settings.navidrome.navidrome_connector.check_credentials", new=AsyncMock(return_value=_OK)) as check:
        authed_client.post(
            "/settings/navidrome",
            data={"base_url": " https://nd.example.com ", "username": " alice ", "password": " pw "},
        )

    assert _payload(db, SOURCE_NAVIDROME) == {"base_url": "https://nd.example.com", "username": "alice", "password": " pw "}
    check.assert_awaited_once_with("https://nd.example.com", "alice", " pw ")


def test_secrets_are_never_echoed_back_to_the_page(authed_client, db):
    with patch("app.routers.settings.plex.plex_connector.check_credentials", new=AsyncMock(return_value=_OK)):
        resp = authed_client.post("/settings/plex", data={"base_url": "http://p:32400", "token": "s3cret-token", "library_id": ""})

    assert "s3cret-token" not in resp.text
