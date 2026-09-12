import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel meaning "nothing was explicitly configured" — never actually used
# to encrypt anything at runtime (see _resolve_secret_key below), just the
# value to compare against. This exact string is public (it's right here, in
# this project's own source on GitHub), which is precisely why it's only ever
# a detection sentinel and not a real fallback key.
DEFAULT_SECRET_KEY = "dev-only-insecure-key-change-me"

SECRET_KEY_FILENAME = ".secret_key"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_secret_key: str = DEFAULT_SECRET_KEY
    app_password: str = ""
    # Optional. Prometheus can't do an interactive login, so /metrics is exempt from the
    # session-cookie gate (see deps.py) regardless — this only adds a bearer-token check
    # on top when you want the scrape endpoint itself restricted (e.g. exposed beyond a
    # trusted LAN). Empty means /metrics is unauthenticated, same "insecure but explicit"
    # default this app already uses for APP_PASSWORD.
    metrics_token: str = ""
    # Marks the session/CSRF cookies Secure. Only turn this on once a reverse proxy
    # is actually terminating real HTTPS in front of this app — browsers refuse to
    # send a Secure cookie back over plain HTTP, so enabling this without HTTPS in
    # front locks out every login. See README's "Reverse proxy / HTTPS" section.
    behind_https_proxy: bool = False

    # SQLite (default) or PostgreSQL — e.g. "postgresql+psycopg://user:pass@host/db"
    # (requires the optional `postgres` extra; see README's "Using PostgreSQL
    # instead of SQLite"). app/db.py branches on the real backend name to pick
    # connection options; app/backup.py's backups feature only works on SQLite.
    database_url: str = "sqlite:///./data/humble.db"
    data_dir: Path = Path("./data")
    downloads_dir: Path = Path("./data/downloads")
    download_concurrency: int = 2

    # The folder-scan feature (routers/downloads.py) can only ever see this
    # directory and its subdirectories — never an arbitrary path from a
    # request — so it's a real containment boundary, not just a convention.
    # In Docker this is a dedicated, admin-chosen, read-only mount (see
    # docker-compose.yml's SCAN_ROOT); the default only makes sense inside
    # that container, so local/non-Docker runs need to override this to a
    # real directory to use the scan feature at all.
    scan_root_dir: Path = Path("/scan-root")

    humble_cli_path: str = "/usr/local/bin/humble-cli"
    # In the container this resolves under $HOME=/root (the Dockerfile runs as root and
    # sets this explicitly — see its own comment on why) — the exact path humble-cli
    # itself hardcodes. MUST be overridden via env var for any local/dev run outside
    # Docker so it never collides with a real ~/.humble-cli-key on the host machine
    # running this code.
    humble_cli_key_path: Path = Path.home() / ".humble-cli-key"

    # When true, the Humble/Steam/GOG connectors talk to mock_api_base_url instead
    # of the real APIs — a fake but realistic backend (see mock_api/), so the app
    # can be explored fully populated without connecting any real account. Off by
    # default; see README's "Try it without connecting accounts" section.
    demo_mode: bool = False
    mock_api_base_url: str = "http://mock-api:8090"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.downloads_dir, self.data_dir / "backups"):
            d.mkdir(parents=True, exist_ok=True)


def _resolve_secret_key(configured: str, data_dir: Path) -> str:
    """An explicitly-configured value always wins outright. Otherwise, rather
    than silently running on DEFAULT_SECRET_KEY (a value that's public
    knowledge, sitting right here in this file on GitHub), generate a real
    random key the first time this ever runs and persist it in the data
    volume so every later boot reuses the same one — the same approach Gitea
    and similar self-hosted tools take. This means a deployment with zero
    secret-key configuration at all is safe by default instead of merely
    "warned about" — see cli.py's rotate_secret_key() for how rotating this
    auto-generated key differs from rotating an explicitly-configured one.

    Blank counts as "not configured" the same as the literal placeholder —
    a real .env file (or a Portainer stack) can easily carry an *empty*
    APP_SECRET_KEY=  through to here, which must trigger generation exactly
    like an entirely-unset one, not get treated as "the user chose an empty
    key."
    """
    if configured and configured != DEFAULT_SECRET_KEY:
        return configured

    key_path = data_dir / SECRET_KEY_FILENAME
    if key_path.exists():
        existing = key_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    generated = secrets.token_hex(32)
    data_dir.mkdir(parents=True, exist_ok=True)
    key_path.write_text(generated, encoding="utf-8")
    os.chmod(key_path, 0o600)
    print(f"Generated a new APP_SECRET_KEY and saved it to {key_path} — back this up alongside your data directory.")
    return generated


settings = Settings()
settings.app_secret_key = _resolve_secret_key(settings.app_secret_key, settings.data_dir)
