from unittest.mock import AsyncMock, patch

import pytest

from app.connectors.navidrome_connector import NavidromeAlbumData
from app.models.credential import SOURCE_NAVIDROME, STATUS_ERROR, STATUS_OK, Credential
from app.models.navidrome_album import NavidromeAlbum
from app.security import encrypt_json
from app.sync import navidrome_sync


def _connect(db):
    db.add(
        Credential(
            source=SOURCE_NAVIDROME,
            encrypted_payload=encrypt_json({"base_url": "https://nd.example.com", "username": "alice", "password": "secret"}),
            status=STATUS_OK,
        )
    )
    db.commit()


@pytest.mark.asyncio
async def test_refresh_raises_when_not_connected(db):
    with pytest.raises(navidrome_sync.NotConnectedError):
        await navidrome_sync.refresh_navidrome_library(db)


@pytest.mark.asyncio
async def test_refresh_upserts_albums_and_sets_status_ok(db):
    _connect(db)
    albums = [NavidromeAlbumData(album_id="al1", name="An Album", artist="An Artist")]
    with patch("app.sync.navidrome_sync.navidrome_connector.fetch_albums", new=AsyncMock(return_value=albums)):
        count = await navidrome_sync.refresh_navidrome_library(db)

    assert count == 1
    saved = db.get(NavidromeAlbum, "al1")
    assert saved.name == "An Album"
    db.expire_all()
    assert Credential.get(db, SOURCE_NAVIDROME).status == STATUS_OK


@pytest.mark.asyncio
async def test_refresh_replaces_stale_albums_no_longer_present(db):
    _connect(db)
    db.add(NavidromeAlbum(album_id="stale", name="Old Album", artist="X"))
    db.commit()

    albums = [NavidromeAlbumData(album_id="al1", name="An Album", artist="An Artist")]
    with patch("app.sync.navidrome_sync.navidrome_connector.fetch_albums", new=AsyncMock(return_value=albums)):
        await navidrome_sync.refresh_navidrome_library(db)

    assert db.get(NavidromeAlbum, "stale") is None
    assert db.get(NavidromeAlbum, "al1") is not None


@pytest.mark.asyncio
async def test_refresh_records_error_status_on_failure(db):
    _connect(db)
    with patch(
        "app.sync.navidrome_sync.navidrome_connector.fetch_albums",
        new=AsyncMock(side_effect=ValueError("Wrong username or password.")),
    ):
        with pytest.raises(ValueError):
            await navidrome_sync.refresh_navidrome_library(db)

    db.expire_all()
    cred = Credential.get(db, SOURCE_NAVIDROME)
    assert cred.status == STATUS_ERROR
    assert "Wrong username" in cred.last_error


def test_owned_normalized_albums_pairs_name_with_artist_and_skips_blanks(db):
    db.add(NavidromeAlbum(album_id="1", name="Some Album", artist="Some Artist"))
    db.add(NavidromeAlbum(album_id="2", name="", artist="Ignored"))
    db.commit()
    assert navidrome_sync.owned_normalized_albums(db) == {("somealbum", "someartist")}
