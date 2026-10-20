from unittest.mock import AsyncMock, patch

import pytest

from app.connectors.plex_connector import PlexAudiobookItemData
from app.models.credential import SOURCE_PLEX, STATUS_ERROR, STATUS_OK, Credential
from app.models.plex_audiobook_item import PlexAudiobookItem
from app.security import encrypt_json
from app.sync import plex_sync


def _connect(db):
    db.add(
        Credential(
            source=SOURCE_PLEX,
            encrypted_payload=encrypt_json({"base_url": "http://plex.example.com:32400", "token": "tok", "library_id": "3"}),
            status=STATUS_OK,
        )
    )
    db.commit()


@pytest.mark.asyncio
async def test_refresh_raises_when_not_connected(db):
    with pytest.raises(plex_sync.NotConnectedError):
        await plex_sync.refresh_plex_library(db)


@pytest.mark.asyncio
async def test_refresh_upserts_items_and_sets_status_ok(db):
    _connect(db)
    items = [PlexAudiobookItemData(rating_key="100", title="A Book", author="An Author")]
    with patch("app.sync.plex_sync.plex_connector.fetch_audiobook_items", new=AsyncMock(return_value=items)):
        count = await plex_sync.refresh_plex_library(db)

    assert count == 1
    saved = db.get(PlexAudiobookItem, "100")
    assert saved.title == "A Book"
    db.expire_all()
    assert Credential.get(db, SOURCE_PLEX).status == STATUS_OK


@pytest.mark.asyncio
async def test_refresh_replaces_stale_items_no_longer_present(db):
    _connect(db)
    db.add(PlexAudiobookItem(rating_key="stale", title="Old Book", author="X"))
    db.commit()

    items = [PlexAudiobookItemData(rating_key="100", title="A Book", author="An Author")]
    with patch("app.sync.plex_sync.plex_connector.fetch_audiobook_items", new=AsyncMock(return_value=items)):
        await plex_sync.refresh_plex_library(db)

    assert db.get(PlexAudiobookItem, "stale") is None
    assert db.get(PlexAudiobookItem, "100") is not None


@pytest.mark.asyncio
async def test_refresh_records_error_status_on_failure(db):
    _connect(db)
    with patch(
        "app.sync.plex_sync.plex_connector.fetch_audiobook_items",
        new=AsyncMock(side_effect=ValueError("Plex rejected this token.")),
    ):
        with pytest.raises(ValueError):
            await plex_sync.refresh_plex_library(db)

    db.expire_all()
    cred = Credential.get(db, SOURCE_PLEX)
    assert cred.status == STATUS_ERROR
    assert "rejected" in cred.last_error


def test_owned_normalized_titles_reflects_the_cached_table_and_skips_blanks(db):
    # Same normalize() as app.audible.scan — casefold, alnum only, matching
    # its own "Dune: Part One" example.
    db.add(PlexAudiobookItem(rating_key="1", title="Dune: Part One", author="A"))
    db.add(PlexAudiobookItem(rating_key="2", title="", author="B"))
    db.commit()
    assert plex_sync.owned_normalized_titles(db) == {"dunepartone"}
