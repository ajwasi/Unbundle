"""One answer to "is each external source working?".

Sources you log in to already carry their state on their `Credential`
(status + last_error). Public scrapes have no credential to hang that on — the
Humble storefront above all, which breaks whenever Humble changes its markup —
so they record into `SourceHealth` instead. `overview()` lays both out in the
same shape for the Settings page, and `record_*` is what a call site uses to
report an outcome.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.credential import (
    SOURCE_AMAZON_MUSIC,
    SOURCE_AUDIBLE,
    SOURCE_AUDIOBOOKSHELF,
    SOURCE_CHIRP,
    SOURCE_GOG,
    SOURCE_HUMBLE,
    SOURCE_NAVIDROME,
    SOURCE_PLEX,
    SOURCE_STEAM,
    STATUS_ERROR,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    Credential,
)
from app.models.source_health import SourceHealth
from app.timeutil import utcnow

SOURCE_STOREFRONT = "humble-storefront"  # the public bundle listing scraped for Home
SOURCE_GOG_DEALS = "gog-deals"  # GOG's public catalogue, scraped for the Deals page
SOURCE_WISHLIST_STEAM = "wishlist-steam"
SOURCE_WISHLIST_GOG = "wishlist-gog"
SOURCE_WISHLIST_CHIRP = "wishlist-chirp"

# Don't write on every successful page view — a healthy source is re-confirmed
# at most this often.
_REFRESH_AFTER = timedelta(minutes=10)

_CREDENTIAL_LABELS = {
    SOURCE_HUMBLE: "Humble Bundle (your library)",
    SOURCE_STEAM: "Steam",
    SOURCE_GOG: "GOG",
    SOURCE_AUDIBLE: "Audible",
    SOURCE_AMAZON_MUSIC: "Amazon Music",
    SOURCE_CHIRP: "Chirp",
    SOURCE_AUDIOBOOKSHELF: "AudiobookShelf",
    SOURCE_PLEX: "Plex",
    SOURCE_NAVIDROME: "Navidrome",
}
_SCRAPE_LABELS = {
    SOURCE_STOREFRONT: "Humble storefront (bundles for sale)",
    SOURCE_GOG_DEALS: "GOG deals (catalogue)",
    SOURCE_WISHLIST_STEAM: "Steam wishlist",
    SOURCE_WISHLIST_GOG: "GOG wishlist",
    SOURCE_WISHLIST_CHIRP: "Chirp wishlist",
}
# Always listed, even before the first check. The rest only appear once they
# have reported an outcome — a user who never opens Deals or syncs a Chirp
# wishlist shouldn't see "not checked yet" rows for them.
_ALWAYS_SHOWN = {SOURCE_STOREFRONT}


def get(db: Session, source: str) -> SourceHealth | None:
    return db.get(SourceHealth, source)


def record_success(db: Session, source: str, at: datetime | None = None) -> None:
    """Report that `source` worked. Cheap to call on every request.

    `at` is when it actually worked, if that isn't now — a page served from a
    cache is evidence of an earlier fetch, not a fresh one.
    """
    when = at or utcnow()
    row = get(db, source)
    if row is None:
        row = SourceHealth(source=source, consecutive_failures=0)
        db.add(row)
    elif row.consecutive_failures == 0 and row.last_success_at and when - row.last_success_at < _REFRESH_AFTER:
        return
    row.last_success_at = when
    row.consecutive_failures = 0
    row.last_error = None
    db.commit()


def record_failure(db: Session, source: str, error: object) -> None:
    """Report that `source` just failed, and why. Keeps the last success time."""
    row = get(db, source)
    if row is None:
        row = SourceHealth(source=source, consecutive_failures=0)
        db.add(row)
    row.last_failure_at = utcnow()
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.last_error = str(error)[:1000]
    db.commit()


def overview(db: Session) -> list[dict]:
    """One row per source worth showing: connected accounts first (configured
    ones only — an unconfigured source isn't a problem), then public scrapes.

    Each row: label, state ("ok" | "error" | "off" — the badge() kinds),
    detail (the error, or ""), last_ok (datetime | None).
    """
    rows = []
    credentials = {c.source: c for c in db.query(Credential).filter(Credential.source.in_(_CREDENTIAL_LABELS)).all()}
    for source, label in _CREDENTIAL_LABELS.items():
        cred = credentials.get(source)
        if cred is None or cred.status == STATUS_NOT_CONFIGURED:
            continue
        state = "ok" if cred.status == STATUS_OK else "error" if cred.status == STATUS_ERROR else "off"
        rows.append(
            {
                "label": label,
                "state": state,
                "detail": (cred.last_error or "") if state == "error" else "",
                "last_ok": cred.updated_at if state == "ok" else None,
            }
        )

    for source, label in _SCRAPE_LABELS.items():
        health = get(db, source)
        if health is None:
            if source not in _ALWAYS_SHOWN:
                continue
            rows.append({"label": label, "state": "off", "detail": "Not checked yet.", "last_ok": None})
            continue
        failing = health.consecutive_failures > 0
        rows.append(
            {
                "label": label,
                "state": "error" if failing else "ok",
                "detail": (health.last_error or "") if failing else "",
                "last_ok": health.last_success_at,
            }
        )
    return rows
