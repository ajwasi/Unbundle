import os

import pytest

from app import humble_key
from app.models.credential import SOURCE_HUMBLE, STATUS_OK, Credential
from app.security import encrypt_json

KEY = "real-session-cookie-value"


@pytest.fixture
def key_path(tmp_path, monkeypatch):
    path = tmp_path / "keydir" / ".humble-cli-key"
    monkeypatch.setattr("app.config.settings.humble_cli_key_path", path)
    return path


def _connect_humble(db, session_key=KEY):
    db.add(Credential(source=SOURCE_HUMBLE, status=STATUS_OK, encrypted_payload=encrypt_json({"session_key": session_key})))
    db.commit()


def test_write_creates_the_directory_and_locks_the_file_down(key_path):
    humble_key.write_key_file(KEY)

    assert key_path.read_text(encoding="utf-8") == KEY
    if os.name != "nt":  # Windows does not model POSIX mode bits
        assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_restores_the_file_when_a_container_recreation_wiped_it(db, key_path):
    # The exact failure: credential survives in /data, key file does not.
    _connect_humble(db)
    assert not key_path.exists()

    assert humble_key.sync_key_file_from_db(db) is True
    assert key_path.read_text(encoding="utf-8") == KEY


def test_does_nothing_when_the_file_already_matches(db, key_path):
    _connect_humble(db)
    humble_key.sync_key_file_from_db(db)

    assert humble_key.sync_key_file_from_db(db) is False


def test_rewrites_a_stale_file(db, key_path):
    _connect_humble(db)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("an-old-key", encoding="utf-8")

    assert humble_key.sync_key_file_from_db(db) is True
    assert key_path.read_text(encoding="utf-8") == KEY


def test_no_credential_is_an_ordinary_state_not_an_error(db, key_path):
    assert humble_key.sync_key_file_from_db(db) is False
    assert not key_path.exists()


def test_a_credential_with_no_session_key_writes_nothing(db, key_path):
    db.add(Credential(source=SOURCE_HUMBLE, status=STATUS_OK, encrypted_payload=encrypt_json({})))
    db.commit()

    assert humble_key.sync_key_file_from_db(db) is False
    assert not key_path.exists()


def test_saving_the_key_in_settings_still_writes_the_file(authed_client, db, key_path):
    from unittest.mock import AsyncMock, patch

    from app.connectors.types import CredentialStatus

    ok = CredentialStatus(ok=True, message="Connected")
    with patch("app.routers.settings.HumbleConnector.check_credentials", new=AsyncMock(return_value=ok)):
        resp = authed_client.post("/settings/humble-key", data={"session_key": KEY})

    assert resp.status_code == 200
    assert key_path.read_text(encoding="utf-8") == KEY
