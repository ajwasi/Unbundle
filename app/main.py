import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import DEFAULT_SECRET_KEY, settings
from app.db import SessionLocal
from app.deps import AuthMiddleware
from app.downloads import worker
from app.oidc import is_auth_configured
from app.routers import auth, bundles, catalog, finance, gog, home, metrics, steam, tags, settings as settings_router
from app.sync import refresh
from app.telemetry import instrument_app

STATIC_DIR = Path(__file__).parent / "static"


def _startup_warnings() -> list[str]:
    """Both checks mirror the exact conditions that make this app's data
    genuinely unprotected — see deps.py's AuthMiddleware and security.py's
    Fernet key derivation. Printed loudly on every boot rather than only
    documented, since neither failure mode is otherwise visible anywhere
    (the app starts and looks fully configured either way).
    """
    warnings = []
    if settings.app_secret_key == DEFAULT_SECRET_KEY:
        warnings.append(
            "APP_SECRET_KEY is left at its default value. Every stored credential "
            "(Humble, Steam, GOG, OIDC) is encrypted with a key derived from this — "
            "and the default is public, sitting in this project's own source on "
            "GitHub. Anyone who gets the database file can decrypt them. Set a real "
            "random value before connecting any real account."
        )
    db = SessionLocal()
    try:
        if not is_auth_configured(db):
            warnings.append(
                "No login is configured (APP_PASSWORD is unset and OIDC isn't "
                "enabled). This app has no authentication at all right now — "
                "anyone who can reach it has full access to everything in it."
            )
    finally:
        db.close()
    return warnings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    for warning in _startup_warnings():
        print(f"WARNING: {warning}", file=sys.stderr)
    refresh.sweep_stale_runs()
    worker.sweep_stale_jobs()
    yield


app = FastAPI(title="Humble Tracker", lifespan=lifespan)
instrument_app(app)  # wraps ASGI middleware for http.server.* metrics — before add_middleware below
app.add_middleware(AuthMiddleware)
# Short-lived signed-cookie session used only to hold the OIDC handshake's state/nonce
# (authlib's requirement) and the post-login redirect target — unrelated to and separate
# from SESSION_COOKIE_NAME, which is this app's own long-lived auth session.
app.add_middleware(SessionMiddleware, secret_key=settings.app_secret_key, session_cookie="humble_tracker_oidc_state")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(settings_router.router)
app.include_router(home.router)
app.include_router(bundles.router)
app.include_router(catalog.router)
app.include_router(finance.router)
app.include_router(steam.router)
app.include_router(gog.router)
app.include_router(tags.router)
app.include_router(metrics.router)
