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

import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.connectors import chirp_connector
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.chirp_series_book import ChirpSeriesBook
from app.models.credential import SOURCE_CHIRP, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json

logger = logging.getLogger(__name__)

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


async def _fetch_series_rows(client, books) -> list[ChirpSeriesBook]:
    """One book page and one series page per distinct series the library
    touches. A series that fails to load is skipped rather than failing the
    whole refresh — the library itself is still good.
    """
    representative: dict[str, str] = {}
    for b in books:
        if b.series_name and b.series_name not in representative:
            representative[b.series_name] = b.url_path

    rows: list[ChirpSeriesBook] = []
    for series_name, book_url_path in representative.items():
        try:
            series_url = await chirp_connector.fetch_series_url_for_book(client, book_url_path)
            if not series_url:
                logger.info("chirp(series): %s's book page (%s) named no series — skipping", series_name, book_url_path)
                continue
            for entry in await chirp_connector.fetch_series_books(client, series_url):
                rows.append(
                    ChirpSeriesBook(
                        series_url=series_url,
                        url_path=entry.url_path,
                        series_name=series_name,
                        title=entry.title,
                        authors=entry.authors,
                        series_number=entry.series_number,
                        listing_price=entry.listing_price,
                        current_price=entry.current_price,
                        fetched_at=datetime.utcnow(),
                    )
                )
        except (chirp_connector.ChirpRequestError, httpx.HTTPError) as exc:
            logger.warning("chirp(series): failed to load series %r via %s: %s", series_name, book_url_path, exc)
            continue
    return rows


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

    previous = {row.url_path: (row.purchased_at, row.paid_price) for row in db.query(ChirpAudiobook).all()}
    try:
        async with chirp_connector.client_from_cookie_header(cookie) as client:
            books, _total = await _fetch_all_pages(client)
            try:
                purchases = await chirp_connector.fetch_purchases(client)
            except (chirp_connector.ChirpRequestError, httpx.HTTPError) as exc:
                logger.warning("chirp: order history unavailable, keeping previous purchase data: %s", exc)
                purchases = None
            series_rows = await _fetch_series_rows(client, books)
    except Exception as exc:
        _set_credential_status(db, STATUS_ERROR, str(exc))
        raise

    earliest = _earliest_purchase_by_url(purchases) if purchases is not None else previous
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

    db.query(ChirpSeriesBook).delete()
    seen_series_keys: set[tuple[str, str]] = set()
    for row in series_rows:
        key = (row.series_url, row.url_path)
        if key in seen_series_keys:
            continue
        seen_series_keys.add(key)
        db.add(row)
    db.commit()

    _set_credential_status(db, STATUS_OK, None)
    return len(books)


def _set_credential_status(db: Session, status: str, error: str | None) -> None:
    Credential.set_status(db, SOURCE_CHIRP, status, error)
