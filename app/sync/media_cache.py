"""The shared shape of a "media server presence cache" refresh.

AudiobookShelf, Plex and Navidrome each cache a flat list of what the server
holds, purely so the Audible/Amazon Music pages can show an "In <server>"
badge. Every refresh is the same sequence — fetch with the saved credential,
record failure on the credential row and re-raise, otherwise replace the
whole cache table and record success — so that sequence lives here and each
`<server>_sync.py` supplies only what's genuinely its own: how to fetch and
how to turn an item into a row.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.credential import STATUS_ERROR, STATUS_OK, Credential
from app.timeutil import utcnow


async def refresh_media_cache(
    db: Session,
    *,
    source: str,
    payload: dict | None,
    not_connected: Exception,
    model: type,
    fetch: Callable[[dict], Awaitable[list]],
    to_row: Callable[[Any, datetime], Any],
) -> int:
    """Returns the number of items fetched. Raises `not_connected` when there
    is no saved credential, or whatever `fetch` raises (also recorded on the
    credential row so Settings shows it) — the caller surfaces the message.
    """
    if not payload:
        raise not_connected

    try:
        items = await fetch(payload)
    except Exception as exc:
        Credential.set_status(db, source, STATUS_ERROR, str(exc))
        raise

    db.query(model).delete()
    now = utcnow()
    for item in items:
        db.add(to_row(item, now))
    db.commit()

    Credential.set_status(db, source, STATUS_OK, None)
    return len(items)
