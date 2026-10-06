"""Refreshes the cached Chirp library by walking every page of
currentUserAudiobooks and upserting the result — same manual, on-demand,
delete-and-reinsert shape as steam_sync.py's refresh_steam_library.

Chirp's own API caps each response at ~20 items (confirmed live: the
perPage argument is rejected outright), and whether its page argument
actually advances through the rest or is silently ignored has NOT been
confirmed (see chirp_connector.py's own module docstring) — no second-page
capture exists yet. _fetch_all_pages() is written defensively because of
that: it stops the moment a page stops introducing purchase ids it hasn't
already seen, rather than assuming pagination works. If it doesn't work,
this degrades to exactly today's single-page behavior (first ~20 items)
instead of spinning forever or double-counting the same page.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.connectors import chirp_connector
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.credential import SOURCE_CHIRP, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json

_MAX_PAGES = 50  # far beyond any real library (50 * ~20/page = ~1000 books) — a runaway-loop backstop, not a real limit


class NotConnectedError(Exception):
    """Raised when no Chirp cookie has been saved yet."""


def _stored_cookie(db: Session) -> str | None:
    cred = Credential.get(db, SOURCE_CHIRP)
    if cred is None or not cred.encrypted_payload:
        return None
    cookie = decrypt_json(cred.encrypted_payload).get("cookie", "")
    return cookie or None


async def _fetch_all_pages(client) -> tuple[list[chirp_connector.ChirpAudiobook], int]:
    all_items: list[chirp_connector.ChirpAudiobook] = []
    seen_ids: set[str] = set()
    total = len(all_items)

    for page in range(1, _MAX_PAGES + 1):
        items, total = await chirp_connector.fetch_library_page(client, page=page)
        if not items:
            break
        new_items = [i for i in items if i.purchase_id not in seen_ids]
        if not new_items:
            # Nothing new on this page — either the real end of the library,
            # or `page` isn't advancing anything server-side. Either way,
            # looping further would only ever repeat what's already in hand.
            break
        all_items.extend(new_items)
        seen_ids.update(i.purchase_id for i in new_items)
        if len(all_items) >= total:
            break

    return all_items, total


def _earliest_purchase_by_url(purchases: list[chirp_connector.ChirpPurchase]) -> dict[str, tuple]:
    earliest: dict[str, tuple] = {}
    for p in purchases:
        current = earliest.get(p.url_path)
        if current is None or (p.purchased_at is not None and (current[0] is None or p.purchased_at < current[0])):
            earliest[p.url_path] = (p.purchased_at, p.paid_price)
    return earliest


async def refresh_chirp_library(db: Session) -> int:
    """Returns the number of audiobooks fetched. Raises NotConnectedError or
    chirp_connector.ChirpRequestError/httpx.HTTPError — caller surfaces the
    message."""
    cookie = _stored_cookie(db)
    if not cookie:
        raise NotConnectedError("Chirp is not connected yet — paste your Cookie header value in Settings.")

    try:
        async with chirp_connector.client_from_cookie_header(cookie) as client:
            books, _total = await _fetch_all_pages(client)
            purchases = await chirp_connector.fetch_purchases(client)
    except Exception as exc:
        _set_credential_status(db, STATUS_ERROR, str(exc))
        raise

    earliest = _earliest_purchase_by_url(purchases)
    db.query(ChirpAudiobook).delete()
    now = datetime.utcnow()
    for b in books:
        purchased_at, paid_price = earliest.get(b.url_path, (None, None))
        db.add(
            ChirpAudiobook(
                purchase_id=b.purchase_id,
                audiobook_id=b.audiobook_id,
                title=b.title,
                authors=b.authors,
                narrators=b.narrators,
                url_path=b.url_path,
                cover_url=b.cover_url,
                progress_status=b.progress_status,
                position_percent=b.position_percent,
                playable=b.playable,
                series_name=b.series_name or "",
                series_number=b.series_number or "",
                listing_price=b.listing_price,
                discount_price=b.discount_price,
                purchased_at=purchased_at,
                paid_price=paid_price,
                fetched_at=now,
            )
        )
    db.commit()

    _set_credential_status(db, STATUS_OK, None)
    return len(books)


def _set_credential_status(db: Session, status: str, error: str | None) -> None:
    Credential.set_status(db, SOURCE_CHIRP, status, error)
