"""Fetches the Humble library and upserts Bundle/BundleEntitlement rows.

Deliberately lighter than a full orchestrator: there's exactly one source
today, so there's no matcher/dedup problem yet (that arrives with a phase-2
Steam connector). Downloadable subproducts are *not* persisted as Download
rows here — a bundle's full subproduct list is re-derived from `Bundle.raw_json`
at render time, and a Download row is only ever created when the user actually
clicks "Download" on a specific item (see routers/downloads.py). This mirrors
audiobook-tracker's "nothing runs automatically" philosophy: refresh is a
manual, on-demand action (POST /bundles/refresh), not a background sync.
"""

import asyncio
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.connectors.base import ConnectorAuthError, LogCallback
from app.connectors.humble_connector import HumbleConnector
from app.db import SessionLocal
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_HUMBLE, STATUS_ERROR, STATUS_OK, Credential
from app.models.sync_run import STATUS_FAILED, STATUS_RUNNING, STATUS_SUCCESS, SyncRun
from app.security import decrypt_json

_refresh_lock = asyncio.Lock()


class NotConnectedError(Exception):
    """Raised when no Humble credential has been saved yet."""


def is_refresh_running() -> bool:
    return _refresh_lock.locked()


def latest_run(db: Session) -> SyncRun | None:
    return db.query(SyncRun).order_by(SyncRun.id.desc()).first()


def sweep_stale_runs() -> None:
    """Call on app startup: a container restart mid-refresh would otherwise leave
    a sync_run stuck in 'running' forever (mirrors audiobook-tracker's
    sweep_stale_sync_runs, same reasoning).
    """
    db = SessionLocal()
    try:
        stale = db.query(SyncRun).filter(SyncRun.status == STATUS_RUNNING).all()
        for run in stale:
            run.status = STATUS_FAILED
            run.finished_at = datetime.utcnow()
            run.error_message = "Interrupted by application restart."
        db.commit()
    finally:
        db.close()


async def start_refresh() -> int:
    """Runs the refresh as a background task and returns immediately — confirmed
    necessary since a full refresh can take well over a minute against a large
    real library (550 bundles took ~70s in testing). Raises RuntimeError if a
    refresh is already in progress.
    """
    if _refresh_lock.locked():
        raise RuntimeError("A refresh is already running.")

    db = SessionLocal()
    try:
        run = SyncRun(status=STATUS_RUNNING)
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id
    finally:
        db.close()

    asyncio.create_task(_run_refresh(run_id))
    return run_id


async def _run_refresh(run_id: int) -> None:
    async with _refresh_lock:
        db = SessionLocal()
        try:
            run = db.get(SyncRun, run_id)

            def log(level: str, message: str) -> None:
                pass  # per-line log persistence isn't needed yet; SyncRun's own status/count is enough

            try:
                count = await refresh_library(db, log)
                run.status = STATUS_SUCCESS
                run.bundle_count = count
            except (NotConnectedError, ConnectorAuthError) as exc:
                run.status = STATUS_FAILED
                run.error_message = str(exc)
            except Exception as exc:  # noqa: BLE001 - last-resort guard so sync_run never hangs at 'running'
                run.status = STATUS_FAILED
                run.error_message = f"Unexpected error: {exc}"
            run.finished_at = datetime.utcnow()
            db.commit()
        finally:
            db.close()


def _get_connector(db: Session) -> HumbleConnector:
    cred = db.query(Credential).filter(Credential.source == SOURCE_HUMBLE).one_or_none()
    if cred is None or not cred.encrypted_payload:
        raise NotConnectedError("Humble is not connected yet — add your session key in Settings.")
    return HumbleConnector(decrypt_json(cred.encrypted_payload))


async def refresh_library(db: Session, log: LogCallback) -> int:
    """Returns the number of bundles fetched. Raises ConnectorAuthError if the
    stored session key was rejected (caller should surface a "reconnect" prompt).
    """
    connector = _get_connector(db)

    try:
        bundles = await connector.sync(log)
    except ConnectorAuthError as exc:
        _set_credential_status(db, STATUS_ERROR, str(exc))
        raise
    else:
        _set_credential_status(db, STATUS_OK, None)

    for normalized in bundles:
        bundle = db.get(Bundle, normalized.gamekey)
        if bundle is None:
            bundle = Bundle(gamekey=normalized.gamekey, name=normalized.name, raw_json="{}",
                             fetched_at=datetime.utcnow())
            db.add(bundle)
        bundle.name = normalized.name
        bundle.category = normalized.category
        bundle.subproduct_count = normalized.item_count
        bundle.key_count = len(normalized.entitlements)
        bundle.amount_spent = normalized.amount_spent
        if normalized.purchased_at:
            try:
                bundle.purchased_at = datetime.fromisoformat(normalized.purchased_at)
            except ValueError:
                bundle.purchased_at = None
        bundle.raw_json = json.dumps(normalized.raw_json, default=str)
        bundle.fetched_at = datetime.utcnow()
        db.flush()

        seen_entitlements: dict[tuple, BundleEntitlement] = {}
        for ent in normalized.entitlements:
            dedup_key = (normalized.gamekey, ent.machine_name, ent.keyindex)
            row = seen_entitlements.get(dedup_key)
            if row is None:
                row = (
                    db.query(BundleEntitlement)
                    .filter(
                        BundleEntitlement.gamekey == normalized.gamekey,
                        BundleEntitlement.machine_name == ent.machine_name,
                        BundleEntitlement.keyindex == ent.keyindex,
                    )
                    .one_or_none()
                )
            if row is None:
                row = BundleEntitlement(gamekey=normalized.gamekey, machine_name=ent.machine_name, keyindex=ent.keyindex)
                db.add(row)
            row.key_name = ent.key_name
            row.redeemed_on_humble = ent.redeemed_on_source
            row.steam_app_id = ent.steam_app_id
            row.gog_id = ent.gog_id
            row.raw_json = json.dumps(ent.raw_json, default=str)
            seen_entitlements[dedup_key] = row

    db.commit()
    log("info", f"Humble: refresh complete, {len(bundles)} bundle(s)")
    return len(bundles)


def _set_credential_status(db: Session, status: str, error: str | None) -> None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_HUMBLE).one_or_none()
    if cred:
        cred.status = status
        cred.last_error = error
        db.commit()
