from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.deps import AuthMiddleware
from app.downloads import worker
from app.routers import auth, bundles, catalog, finance, gog, home, steam, settings as settings_router
from app.sync import refresh

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    refresh.sweep_stale_runs()
    worker.sweep_stale_jobs()
    yield


app = FastAPI(title="Humble Tracker", lifespan=lifespan)
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
