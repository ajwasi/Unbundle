"""Test env setup MUST happen before any `app.*` import anywhere in the suite —
app/config.py reads env vars into a module-level Settings() singleton the moment
it's first imported, and app/db.py creates its engine from settings.database_url
at import time too. Pytest always imports the rootmost conftest.py before
collecting any test module, so doing this here (at module level, top of file)
is what guarantees it happens first.

Never points at the user's real ~/.humble-cli-key or dev database — everything
lives under a throwaway temp dir, fresh per test session.
"""

import os
import tempfile
from pathlib import Path

_TEST_DIR = Path(tempfile.mkdtemp(prefix="unbundle_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TEST_DIR / 'test.db').as_posix()}"
os.environ["APP_SECRET_KEY"] = "test-only-secret-key"
os.environ["APP_PASSWORD"] = "test-password"
os.environ["HUMBLE_CLI_KEY_PATH"] = str(_TEST_DIR / ".humble-cli-key-test")
os.environ["DATA_DIR"] = str(_TEST_DIR / "data")
os.environ["DOWNLOADS_DIR"] = str(_TEST_DIR / "downloads")
os.environ["HUMBLE_CLI_PATH"] = str(_TEST_DIR / "fake-humble-cli")

import json  # noqa: E402
from datetime import datetime  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.csrf import require_csrf  # noqa: E402
from app.db import Base, engine, SessionLocal  # noqa: E402
from app.deps import get_db  # noqa: E402

# Import every model module so its table registers on Base.metadata before
# create_all runs below — a model that's never imported anywhere in the process
# would otherwise silently get no table at all.
from app.models import backup_settings, bundle, bundle_entitlement, credential, download, download_destination, download_job, download_settings, steam_game, sync_run, tag  # noqa: E402,F401
from tests.factories import make_order  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_schema():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _no_real_update_checks(monkeypatch):
    """app.main's lifespan starts version.run_update_check_loop(), which calls
    check_for_update() immediately on startup — every test that spins up a
    TestClient (most of this suite) would otherwise make a real network call to
    GitHub's API, violating this suite's zero-network-access rule (and adding
    real, sometimes-slow latency to every single test in the process). Patches
    the loop wrapper, not check_for_update itself — tests/test_version.py calls
    check_for_update() directly and needs the real thing.
    """

    async def _noop():
        return None

    monkeypatch.setattr("app.version.run_update_check_loop", _noop)
    yield


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Rate limiter instances (app/ratelimit.py) are module-level singletons that
    outlive any single test's TestClient — without this, hit counts would
    accumulate across the whole suite (every authed_client fixture use alone posts
    to /login) and eventually start rejecting unrelated tests' requests.
    """
    from app.ratelimit import reset_all

    reset_all()
    yield


@pytest.fixture(autouse=True)
def _fresh_catalog_cache():
    """_build_catalog()'s in-process cache (app/routers/catalog.py) is a
    module-level dict that outlives any single test's isolated DB — without
    this, a cache entry keyed on (bundle count, max(fetched_at)) populated by
    one test could coincidentally match another test's fresh-but-differently-
    seeded DB (same count, same-microsecond-or-mocked fetched_at) and hand
    back the wrong catalog entirely.
    """
    from app.routers import catalog

    catalog._catalog_cache["key"] = None
    catalog._catalog_cache["items"] = None
    yield


@pytest.fixture(autouse=True)
def _fresh_backups_dir():
    """Same reasoning as _fresh_downloads_dir below — settings.data_dir is a
    single process-wide path for the whole test session, so backup files one
    test creates would otherwise still be sitting there for the next test's
    list_backups()/retention assertions.
    """
    import shutil

    from app.config import settings

    backups_dir = settings.data_dir / "backups"
    shutil.rmtree(backups_dir, ignore_errors=True)
    backups_dir.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture(autouse=True)
def _fresh_downloads_dir():
    """settings.downloads_dir is a single process-wide path (set once via env var
    in conftest, since Settings() is a module-level singleton) — without this,
    a file a worker test writes to verify "download succeeded" would still be
    sitting on disk for the next test, which predicts paths the same way from
    bundle/item names and can easily collide (as it did: two tests both using
    the default "Test Bundle/Cool Book/book.epub" fixture data).
    """
    import shutil

    from app.config import settings

    shutil.rmtree(settings.downloads_dir, ignore_errors=True)
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app_instance():
    from app.main import app

    return app


@pytest.fixture
def client(app_instance, db):
    """CSRF enforcement (app/csrf.py) is bypassed by default here, the same way
    get_db is overridden — hundreds of existing tests POST to mutating routes
    without needing to know CSRF machinery exists at all. Tests that actually
    exercise require_csrf itself (tests/test_csrf.py) use raw_client instead,
    which does NOT carry this override.
    """

    def _override_get_db():
        yield db

    app_instance.dependency_overrides[get_db] = _override_get_db
    app_instance.dependency_overrides[require_csrf] = lambda: None
    with TestClient(app_instance) as c:
        yield c
    app_instance.dependency_overrides.clear()


@pytest.fixture
def raw_client(app_instance, db):
    """Like `client`, but WITHOUT the require_csrf override — for tests that
    verify the CSRF mechanism itself actually enforces something.
    """

    def _override_get_db():
        yield db

    app_instance.dependency_overrides[get_db] = _override_get_db
    with TestClient(app_instance) as c:
        yield c
    app_instance.dependency_overrides.clear()


@pytest.fixture
def authed_client(client):
    resp = client.post("/login", data={"password": "test-password", "next": "/"}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return client


@pytest.fixture
def make_bundle(db):
    def _make(gamekey="TESTKEY1", order=None, **overrides):
        order = order if order is not None else make_order()
        purchased_at = None
        if order.get("created"):
            try:
                purchased_at = datetime.fromisoformat(order["created"])
            except ValueError:
                purchased_at = None
        b = bundle.Bundle(
            gamekey=gamekey,
            name=order["product"]["human_name"],
            category=order["product"].get("category", ""),
            subproduct_count=len(order.get("subproducts") or []),
            key_count=len((order.get("tpkd_dict") or {}).get("all_tpks") or []),
            purchased_at=purchased_at,
            amount_spent=float(order.get("amount_spent") or 0.0),
            raw_json=json.dumps(order),
            fetched_at=datetime.utcnow(),
        )
        for k, v in overrides.items():
            setattr(b, k, v)
        db.add(b)
        db.commit()
        db.refresh(b)
        return b

    return _make


@pytest.fixture(scope="session")
def mock_api_server():
    """The real mock_api FastAPI app on a real background HTTP server (see
    tests/mock_server.py) — started once for the whole test session since it's
    fully stateless (every route is a pure function of the query params plus
    the static curated dataset, nothing here ever mutates).
    """
    from tests.mock_server import MockApiServer

    server = MockApiServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def demo_mode(mock_api_server, monkeypatch):
    """Points the real Humble/Steam/GOG connectors at the running mock server
    for the duration of one test — monkeypatch (function-scoped, auto-reverts)
    rather than mutating the shared settings singleton directly, so this can
    never bleed into a test that didn't ask for it.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "mock_api_base_url", mock_api_server.base_url)
    yield mock_api_server
