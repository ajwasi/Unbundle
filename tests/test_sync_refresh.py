from unittest.mock import AsyncMock, patch

import pytest

from app.connectors.base import ConnectorAuthError
from app.connectors.humble_connector import parse_bundle
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_HUMBLE, STATUS_ERROR, STATUS_OK, Credential
from app.security import encrypt_json
from app.sync import refresh
from tests.factories import make_order, make_tpk


def _connect_humble(db):
    db.add(Credential(source=SOURCE_HUMBLE, encrypted_payload=encrypt_json({"session_key": "abc"}), status=STATUS_OK))
    db.commit()


@pytest.mark.asyncio
async def test_refresh_library_raises_when_not_connected(db):
    with pytest.raises(refresh.NotConnectedError):
        await refresh.refresh_library(db, lambda level, msg: None)


@pytest.mark.asyncio
async def test_refresh_library_creates_new_bundle(db):
    _connect_humble(db)
    normalized = [parse_bundle("GK1", make_order(name="New Bundle", amount_spent=9.99))]
    with patch("app.connectors.humble_connector.HumbleConnector.sync", new=AsyncMock(return_value=normalized)):
        count = await refresh.refresh_library(db, lambda level, msg: None)

    assert count == 1
    saved = db.get(Bundle, "GK1")
    assert saved is not None
    assert saved.name == "New Bundle"
    assert saved.amount_spent == 9.99


@pytest.mark.asyncio
async def test_refresh_library_updates_existing_bundle_in_place(db, make_bundle):
    _connect_humble(db)
    make_bundle(gamekey="GK2", order=make_order(name="Old Name", amount_spent=5.0))
    normalized = [parse_bundle("GK2", make_order(name="Updated Name", amount_spent=12.0))]
    with patch("app.connectors.humble_connector.HumbleConnector.sync", new=AsyncMock(return_value=normalized)):
        await refresh.refresh_library(db, lambda level, msg: None)

    saved = db.get(Bundle, "GK2")
    assert saved.name == "Updated Name"
    assert saved.amount_spent == 12.0


@pytest.mark.asyncio
async def test_refresh_library_marks_credential_ok_on_success(db):
    _connect_humble(db)
    with patch("app.connectors.humble_connector.HumbleConnector.sync", new=AsyncMock(return_value=[])):
        await refresh.refresh_library(db, lambda level, msg: None)
    cred = db.query(Credential).filter(Credential.source == SOURCE_HUMBLE).one()
    assert cred.status == STATUS_OK


@pytest.mark.asyncio
async def test_refresh_library_marks_credential_error_on_auth_failure(db):
    _connect_humble(db)
    with patch("app.connectors.humble_connector.HumbleConnector.sync", new=AsyncMock(side_effect=ConnectorAuthError("rejected"))):
        with pytest.raises(ConnectorAuthError):
            await refresh.refresh_library(db, lambda level, msg: None)
    cred = db.query(Credential).filter(Credential.source == SOURCE_HUMBLE).one()
    assert cred.status == STATUS_ERROR
    assert cred.last_error == "rejected"


@pytest.mark.asyncio
async def test_refresh_library_dedups_entitlements_by_gamekey_machine_name_keyindex(db):
    # Confirmed against a real account: (gamekey, key_name) alone is NOT unique —
    # the same game can be granted twice with an identical human-readable name.
    _connect_humble(db)
    order = make_order(
        subproducts=[],
        tpks=[
            make_tpk(human_name="Duplicate Name", machine_name="game1", keyindex=0),
            make_tpk(human_name="Duplicate Name", machine_name="game1", keyindex=1),
        ],
    )
    normalized = [parse_bundle("GK3", order)]
    with patch("app.connectors.humble_connector.HumbleConnector.sync", new=AsyncMock(return_value=normalized)):
        await refresh.refresh_library(db, lambda level, msg: None)

    rows = db.query(BundleEntitlement).filter(BundleEntitlement.gamekey == "GK3").all()
    assert len(rows) == 2
    assert {r.keyindex for r in rows} == {0, 1}


@pytest.mark.asyncio
async def test_refresh_library_upserts_entitlement_on_second_run(db):
    _connect_humble(db)
    order_v1 = make_order(subproducts=[], tpks=[make_tpk(human_name="V1", machine_name="game1", keyindex=0)])
    with patch("app.connectors.humble_connector.HumbleConnector.sync", new=AsyncMock(return_value=[parse_bundle("GK4", order_v1)])):
        await refresh.refresh_library(db, lambda level, msg: None)

    order_v2 = make_order(subproducts=[], tpks=[make_tpk(human_name="V2", machine_name="game1", keyindex=0)])
    with patch("app.connectors.humble_connector.HumbleConnector.sync", new=AsyncMock(return_value=[parse_bundle("GK4", order_v2)])):
        await refresh.refresh_library(db, lambda level, msg: None)

    rows = db.query(BundleEntitlement).filter(BundleEntitlement.gamekey == "GK4").all()
    assert len(rows) == 1
    assert rows[0].key_name == "V2"


def test_sweep_stale_runs_marks_running_as_failed(db):
    from app.models.sync_run import STATUS_FAILED, STATUS_RUNNING, SyncRun

    run = SyncRun(status=STATUS_RUNNING)
    db.add(run)
    db.commit()
    db.refresh(run)

    refresh.sweep_stale_runs()

    db.refresh(run)
    assert run.status == STATUS_FAILED
    assert run.finished_at is not None


def test_sweep_stale_runs_leaves_completed_runs_alone(db):
    from app.models.sync_run import STATUS_SUCCESS, SyncRun

    run = SyncRun(status=STATUS_SUCCESS, bundle_count=5)
    db.add(run)
    db.commit()
    db.refresh(run)

    refresh.sweep_stale_runs()

    db.refresh(run)
    assert run.status == STATUS_SUCCESS
