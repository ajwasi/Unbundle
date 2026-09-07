"""Schema-level invariants — distinct from the application-level dedup logic
already covered in test_sync_refresh.py/test_bundles_helpers.py: these confirm
the DB itself would still refuse a duplicate row even if some future code path
bypassed the application's own upsert logic.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.download import Download


def _seed_bundle(db, gamekey="GK1"):
    db.add(Bundle(gamekey=gamekey, name="B", raw_json="{}"))
    db.commit()


def test_bundle_entitlement_unique_constraint_is_gamekey_machine_name_keyindex(db):
    _seed_bundle(db)
    db.add(BundleEntitlement(gamekey="GK1", machine_name="game1", keyindex=0, key_name="A"))
    db.commit()
    db.add(BundleEntitlement(gamekey="GK1", machine_name="game1", keyindex=0, key_name="Same key again"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_bundle_entitlement_allows_same_machine_name_different_keyindex(db):
    _seed_bundle(db)
    db.add(BundleEntitlement(gamekey="GK1", machine_name="game1", keyindex=0, key_name="Copy 1"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="game1", keyindex=1, key_name="Copy 2"))
    db.commit()  # must not raise — this is exactly the real-account case that broke the old (gamekey, key_name) constraint

    rows = db.query(BundleEntitlement).filter(BundleEntitlement.gamekey == "GK1").all()
    assert len(rows) == 2


def test_download_unique_constraint_is_gamekey_item_name_filename(db):
    _seed_bundle(db)
    db.add(Download(gamekey="GK1", item_name="Item", original_filename="f.epub"))
    db.commit()
    db.add(Download(gamekey="GK1", item_name="Item", original_filename="f.epub"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_download_allows_same_item_different_format_filename(db):
    _seed_bundle(db)
    db.add(Download(gamekey="GK1", item_name="Item", original_filename="f.epub"))
    db.add(Download(gamekey="GK1", item_name="Item", original_filename="f.pdf"))
    db.commit()  # must not raise — same item, two format variants

    rows = db.query(Download).filter(Download.gamekey == "GK1").all()
    assert len(rows) == 2


def test_credential_source_is_unique(db):
    from app.models.credential import Credential

    db.add(Credential(source="humble"))
    db.commit()
    db.add(Credential(source="humble"))
    with pytest.raises(IntegrityError):
        db.commit()
