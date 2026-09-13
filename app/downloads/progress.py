"""Per-bundle "N of M items downloaded" counts for the Bundles list page.

The denominator (Bundle.downloadable_item_count) is denormalized at refresh
time (see sync/refresh.py) specifically so this never has to touch raw_json —
the Bundles list route already defers that column outright (see its own
comment: confirmed live against a real 550-bundle library that reading it
there is the difference between a ~50ms and a ~400ms query), and this module
must never reintroduce that cost.

The numerator counts a subproduct as "downloaded" once *any* one of its file
variants has a completed Download row — a deliberately coarser definition
than the bundle-detail page's own _group_items() ("every" variant must
complete), which needs raw_json to know how many variants a multi-format
item actually has. This list-level summary trades that last bit of per-item
precision for staying entirely SQL-side (two cheap aggregate queries, no
per-bundle JSON parsing at all).
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.bundle import Bundle
from app.models.download import STATUS_COMPLETED, Download


def bundle_download_progress(db: Session) -> dict[str, tuple[int, int]]:
    """gamekey -> (items_downloaded, items_downloadable)."""
    downloaded_by_gamekey = dict(
        db.query(Download.gamekey, func.count(func.distinct(Download.subproduct_index)))
        .filter(Download.status == STATUS_COMPLETED)
        .group_by(Download.gamekey)
        .all()
    )
    return {
        gamekey: (downloaded_by_gamekey.get(gamekey, 0), total)
        for gamekey, total in db.query(Bundle.gamekey, Bundle.downloadable_item_count).all()
    }
