"""Keeps humble-cli's key file in step with the stored Humble credential.

humble-cli reads its session key from a hardcoded `$HOME/.humble-cli-key`,
which in the container is `/home/appuser/.humble-cli-key` — inside the image,
*not* inside the `/data` volume. So recreating the container (every image
update) destroys it, while the encrypted credential in the database survives.
The result is an app that reports Humble as connected while every download
fails with "humble-cli: config file not found".

The file is only ever a cache of something already persisted, so the fix is to
treat it as derived state and rewrite it from the database whenever it has
gone missing or drifted. Startup is the right moment: the failure mode is
container recreation, and that is exactly when this runs.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.config import settings
from app.models.credential import SOURCE_HUMBLE, Credential


def write_key_file(session_key: str) -> None:
    """Write the key in humble-cli's own format: plaintext, 0600."""
    key_path = settings.humble_cli_key_path
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(session_key, encoding="utf-8")
    os.chmod(key_path, 0o600)


def sync_key_file_from_db(db: Session) -> bool:
    """Rewrite the key file when it is missing or stale. True if it wrote.

    Returns False — quietly — when no Humble credential is stored, which is a
    perfectly ordinary state for an instance that has never connected Humble.
    """
    payload = Credential.get_payload(db, SOURCE_HUMBLE)
    if not payload:
        return False

    session_key = payload.get("session_key") or ""
    if not session_key:
        return False

    key_path = settings.humble_cli_key_path
    try:
        if key_path.is_file() and key_path.read_text(encoding="utf-8").strip() == session_key.strip():
            return False
    except OSError:
        # Unreadable is as good as missing here — rewrite it.
        pass

    write_key_file(session_key)
    return True
