from unittest.mock import AsyncMock, patch

import pytest

from app.connectors.audiobookshelf_connector import AudiobookShelfItemData
from app.models.audiobookshelf_item import AudiobookShelfItem
from app.models.credential import SOURCE_AUDIOBOOKSHELF, STATUS_ERROR, STATUS_OK, Credential
from app.security import encrypt_json
from app.sync import audiobookshelf_sync


def _connect(db):
    db.add(
        Credential(
            source=SOURCE_AUDIOBOOKSHELF,
            encrypted_payload=encrypt_json({"base_url": "https://abs.example.com", "api_token": "tok", "library_id": ""}),
            status=STATUS_OK,
        )
    )
    db.commit()


@pytest.mark.asyncio
async def test_refresh_raises_when_not_connected(db):
    with pytest.raises(audiobookshelf_sync.NotConnectedError):
        await audiobookshelf_sync.refresh_audiobookshelf_library(db)


@pytest.mark.asyncio
async def test_refresh_upserts_items_and_sets_status_ok(db):
    _connect(db)
    items = [AudiobookShelfItemData(library_item_id="item1", asin="B001", title="A Book", author="An Author")]
    with patch("app.sync.audiobookshelf_sync.audiobookshelf_connector.fetch_library_items", new=AsyncMock(return_value=items)):
        count = await audiobookshelf_sync.refresh_audiobookshelf_library(db)

    assert count == 1
    saved = db.get(AudiobookShelfItem, "item1")
    assert saved.asin == "B001"
    assert saved.title == "A Book"
    db.expire_all()
    assert Credential.get(db, SOURCE_AUDIOBOOKSHELF).status == STATUS_OK


@pytest.mark.asyncio
async def test_refresh_replaces_stale_items_no_longer_present(db):
    _connect(db)
    db.add(AudiobookShelfItem(library_item_id="stale-item", asin="B999", title="Old", author="X"))
    db.commit()

    items = [AudiobookShelfItemData(library_item_id="item1", asin="B001", title="A Book", author="An Author")]
    with patch("app.sync.audiobookshelf_sync.audiobookshelf_connector.fetch_library_items", new=AsyncMock(return_value=items)):
        await audiobookshelf_sync.refresh_audiobookshelf_library(db)

    assert db.get(AudiobookShelfItem, "stale-item") is None
    assert db.get(AudiobookShelfItem, "item1") is not None


@pytest.mark.asyncio
async def test_refresh_stores_empty_asin_as_null_not_blank_string(db):
    _connect(db)
    items = [AudiobookShelfItemData(library_item_id="item1", asin="", title="No ASIN", author="Author")]
    with patch("app.sync.audiobookshelf_sync.audiobookshelf_connector.fetch_library_items", new=AsyncMock(return_value=items)):
        await audiobookshelf_sync.refresh_audiobookshelf_library(db)

    assert db.get(AudiobookShelfItem, "item1").asin is None
    # owned_asins() must never include a null/blank entry as if it were a real match
    assert audiobookshelf_sync.owned_asins(db) == set()


@pytest.mark.asyncio
async def test_refresh_records_error_status_on_failure(db):
    _connect(db)
    with patch(
        "app.sync.audiobookshelf_sync.audiobookshelf_connector.fetch_library_items",
        new=AsyncMock(side_effect=ValueError("AudiobookShelf rejected this API token.")),
    ):
        with pytest.raises(ValueError):
            await audiobookshelf_sync.refresh_audiobookshelf_library(db)

    db.expire_all()
    cred = Credential.get(db, SOURCE_AUDIOBOOKSHELF)
    assert cred.status == STATUS_ERROR
    assert "rejected" in cred.last_error


def test_owned_asins_reflects_the_cached_table(db):
    db.add(AudiobookShelfItem(library_item_id="item1", asin="B001", title="A", author="A"))
    db.add(AudiobookShelfItem(library_item_id="item2", asin="B002", title="B", author="B"))
    db.commit()
    assert audiobookshelf_sync.owned_asins(db) == {"B001", "B002"}
