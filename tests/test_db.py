from app.db import engine


def test_sqlite_connections_use_wal_journal_mode(db):
    # WAL lets readers proceed against the last-committed snapshot while a
    # writer is active, instead of the default rollback-journal mode's
    # whole-file lock for the duration of any write — see db.py's own
    # comment on the connect-time PRAGMA that sets this.
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode == "wal"
