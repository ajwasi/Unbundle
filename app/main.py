import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import applog, backup, version
from app.config import settings
from app.db import SessionLocal
from app.deps import AuthMiddleware, SecurityHeadersMiddleware
from app.downloads import worker
from app.oidc import is_auth_configured
from app.routers import amazon_music, auth, audible, wishlist, bundles, catalog, docs, downloads, finance, gog, home, metrics, steam, tags, version as version_router, settings as settings_router
from app.sync import refresh
from app.telemetry import instrument_app

STATIC_DIR = Path(__file__).parent / "static"

# Installed at import time, not inside lifespan() — active from the earliest
# possible moment (before any router/connector module below has a chance to
# log anything), rather than waiting for uvicorn to actually start serving.
applog.install()


def _startup_warnings() -> list[str]:
    """Mirrors the exact condition that makes this app's data genuinely
    unprotected — see deps.py's AuthMiddleware. Printed loudly on every boot
    rather than only documented, since this failure mode is otherwise
    invisible (the app starts and looks fully configured either way).

    APP_SECRET_KEY itself no longer needs a check here: app/config.py now
    auto-generates and persists a real random one whenever nothing was
    explicitly configured, so by the time this ever runs, settings.app_secret_key
    can never still be the public DEFAULT_SECRET_KEY placeholder.
    """
    warnings = []
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

    missing = settings.ssl_files_missing()
    if missing:
        warnings.append(
            "SSL_CERTFILE/SSL_KEYFILE point at files that don't exist: "
            f"{', '.join(missing)}. uvicorn will refuse to start until the "
            "certificate is mounted into the container at that exact path."
        )
    if settings.ssl_certfile and settings.behind_https_proxy:
        warnings.append(
            "Both SSL_CERTFILE and BEHIND_HTTPS_PROXY are set. This app is "
            "terminating TLS itself, so the proxy setting is redundant — and "
            "if a proxy really is in front, it must be speaking HTTPS to this "
            "app, not plain HTTP."
        )
    return warnings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    for warning in _startup_warnings():
        print(f"WARNING: {warning}", file=sys.stderr)
    refresh.sweep_stale_runs()
    worker.sweep_stale_jobs()
    await worker.try_dispatch_queued_downloads()  # picks up any jobs left STATUS_QUEUED across a restart

    # The one genuinely perpetual background task in this app — see backup.py's
    # own docstring for why scheduled backups are a deliberate exception to the
    # "nothing runs unless a request triggers it" pattern everything else follows.
    scheduler_task = asyncio.create_task(backup.run_scheduler_loop())
    update_check_task = asyncio.create_task(version.run_update_check_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        update_check_task.cancel()


app = FastAPI(title="Unbundle", lifespan=lifespan, docs_url=None, redoc_url=None)
instrument_app(app)  # wraps ASGI middleware for http.server.* metrics — before add_middleware below
app.add_middleware(AuthMiddleware)
# Short-lived signed-cookie session used only to hold the OIDC handshake's state/nonce
# (authlib's requirement) and the post-login redirect target — unrelated to and separate
# from SESSION_COOKIE_NAME, which is this app's own long-lived auth session.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    session_cookie="unbundle_oidc_state",
    https_only=settings.https_enabled,
)
# Added last so it's the outermost layer — compresses the fully-rendered
# response (pages, htmx fragments, and static JS/CSS) right before it goes
# out, after everything else has finished. Default 500-byte threshold skips
# tiny responses where compression overhead isn't worth it.
app.add_middleware(GZipMiddleware)
# The true outermost layer: added after GZip so these headers land on every
# response this app ever sends — static files, /metrics, login/setup, and
# any error response an inner middleware produces — not just the ones that
# reach a route handler.
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(settings_router.router)
app.include_router(home.router)
app.include_router(bundles.router)
app.include_router(catalog.router)
app.include_router(downloads.router)
app.include_router(finance.router)
app.include_router(steam.router)
app.include_router(gog.router)
app.include_router(audible.router)
app.include_router(amazon_music.router)
app.include_router(wishlist.router)
app.include_router(tags.router)
app.include_router(metrics.router)
app.include_router(version_router.router)
app.include_router(docs.router)
