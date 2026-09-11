"""Confirms the indexes added for gamekey/machine_name lookups that don't
benefit from an existing composite unique index (their column isn't the
leading one — see the migration/model comments in
app/alembic/versions/0014_missing_fk_indexes.py). Model index=True already
makes these present for the test suite's own Base.metadata.create_all()
schema; the Alembic migration is what applies them to an existing real
database that predates this change.
"""

from sqlalchemy import inspect

from app.db import engine


def _indexed_columns(table_name: str) -> set[str]:
    columns = set()
    for index in inspect(engine).get_indexes(table_name):
        columns.update(index["column_names"])
    return columns


def test_download_job_gamekey_is_indexed(db):
    assert "gamekey" in _indexed_columns("download_job")


def test_bundle_tag_gamekey_is_indexed(db):
    assert "gamekey" in _indexed_columns("bundle_tag")


def test_item_tag_machine_name_is_indexed(db):
    assert "machine_name" in _indexed_columns("item_tag")
