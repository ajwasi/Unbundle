from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.connectors.audible_connector import AudibleBookData
from app.models.audible_book import AudibleBook
from app.models.credential import SOURCE_AUDIBLE, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json
from app.sync import audible_sync


class _FakeAuth:
    """A stand-in for audible.Authenticator — save_authenticator() only ever
    calls .to_dict() on whatever from_dict() returns, so that's all a fake
    needs here."""

    def __init__(self, data=None):
        self._data = data or {"access_token": "AT", "locale_code": "us"}

    def to_dict(self):
        return self._data


def _connect_audible(db, payload=None):
    db.add(
        Credential(
            source=SOURCE_AUDIBLE,
            encrypted_payload=encrypt_json(payload or {"access_token": "AT", "locale_code": "us"}),
            status=STATUS_OK,
        )
    )
    db.commit()


@pytest.mark.asyncio
async def test_refresh_audible_library_raises_when_not_connected(db):
    with pytest.raises(audible_sync.NotConnectedError):
        await audible_sync.refresh_audible_library(db)


@pytest.mark.asyncio
async def test_refresh_audible_library_upserts_books(db):
    _connect_audible(db)
    books = [AudibleBookData(asin="B001", title="A Great Book", author="Jane Author", runtime_minutes=605, cover_url="")]
    with patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()):
        with patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=books)):
            count = await audible_sync.refresh_audible_library(db)

    assert count == 1
    saved = db.get(AudibleBook, "B001")
    assert saved.title == "A Great Book"
    assert saved.author == "Jane Author"


@pytest.mark.asyncio
async def test_refresh_audible_library_full_replace_on_each_refresh(db):
    _connect_audible(db)
    db.add(AudibleBook(asin="STALE", title="No Longer Owned", fetched_at=datetime.utcnow()))
    db.commit()

    books = [AudibleBookData(asin="B001", title="A Great Book", author="", runtime_minutes=0, cover_url="")]
    with patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()):
        with patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=books)):
            await audible_sync.refresh_audible_library(db)

    assert db.get(AudibleBook, "STALE") is None
    assert db.get(AudibleBook, "B001") is not None


@pytest.mark.asyncio
async def test_refresh_audible_library_persists_the_refreshed_authenticator(db):
    _connect_audible(db, payload={"access_token": "OLD", "locale_code": "us"})

    with patch(
        "app.sync.audible_sync.audible.Authenticator.from_dict",
        return_value=_FakeAuth({"access_token": "NEW", "locale_code": "us"}),
    ):
        with patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=[])):
            await audible_sync.refresh_audible_library(db)

    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one()
    assert decrypt_json(cred.encrypted_payload)["access_token"] == "NEW"


@pytest.mark.asyncio
async def test_refresh_audible_library_records_error_on_failure(db):
    _connect_audible(db)
    with patch("app.sync.audible_sync.audible.Authenticator.from_dict", side_effect=ValueError("boom")):
        with pytest.raises(ValueError):
            await audible_sync.refresh_audible_library(db)

    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one()
    assert cred.status == STATUS_ERROR
    assert "boom" in cred.last_error
