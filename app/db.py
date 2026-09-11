from collections.abc import Generator

from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# Real backend name, not a raw URL-prefix string match — this is the same
# introspection SQLAlchemy itself uses (see backup.py's engine.url calls),
# so both places agree by construction. Note the trailing "ql": SQLAlchemy's
# backend name for Postgres is "postgresql", never "postgres" — a typo here
# would silently fall through to the "anything else" branch below.
_backend = make_url(settings.database_url).get_backend_name()

connect_args: dict = {}
engine_kwargs: dict = {}
if _backend == "sqlite":
    connect_args = {"check_same_thread": False}
elif _backend == "postgresql":
    # Avoids stale-connection errors if Postgres restarts (or drops idle
    # connections) while this process keeps running — cheap at this app's
    # scale (single admin, no concurrent load), so hardcoded rather than a
    # setting. Irrelevant for SQLite (no server process to restart).
    engine_kwargs["pool_pre_ping"] = True
# else: unrecognized backend — don't assume anything, same as before.

engine = create_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
