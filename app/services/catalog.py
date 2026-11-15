"""The cross-bundle item catalog: every distinct item across every purchased
bundle, flattened, keyed by subproduct `machine_name`, with per-bundle price,
date and "downloaded" state. Used by the Catalog pages, the Home/Deals
"already owned" ratios and the compare view — which is why it lives here and
not in routers/catalog.py (routers shouldn't import each other's helpers).

Computed from Bundle.raw_json rather than a persisted table. `build_catalog()`
is cached in-process (see `_catalog_cache`): parsing and grouping ~550 bundles'
JSON measured at ~0.3-0.4s, cheap for one page load but paid on every
infinite-scroll batch without the cache. It is keyed on (bundle count,
max(fetched_at)) and (download count, max(completed_at)) rather than tied to a
mutation call site, so it self-invalidates whenever sync or a download changes
the data.
"""

import json

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.connectors.humble_connector import parse_bundle
from app.models.bundle import Bundle
from app.models.download import STATUS_COMPLETED, Download


_catalog_cache: dict = {"key": None, "items": None}


def _catalog_cache_key(db: Session) -> tuple:
    # Bundle data and Download rows change on independent schedules (a
    # refresh vs. a completed download) — both must invalidate this cache, or
    # each bundle-copy's "downloaded" flag below would go stale the moment a
    # download completes without a bundle refresh alongside it.
    bundle_key = db.query(func.count(Bundle.gamekey), func.max(Bundle.fetched_at)).one()
    download_key = db.query(func.count(Download.id), func.max(Download.completed_at)).one()
    return (bundle_key, download_key)


def _tracked_by_gamekey(db: Session) -> dict[str, dict[tuple[str, str], str]]:
    tracked: dict[str, dict[tuple[str, str], str]] = {}
    for gamekey, item_name, filename, status in db.query(
        Download.gamekey, Download.item_name, Download.original_filename, Download.status
    ):
        tracked.setdefault(gamekey, {})[(item_name, filename)] = status
    return tracked


def build_catalog(db: Session) -> dict[str, dict]:
    key = _catalog_cache_key(db)
    if _catalog_cache["key"] == key:
        return _catalog_cache["items"]

    tracked_by_gamekey = _tracked_by_gamekey(db)

    items: dict[str, dict] = {}
    for bundle in db.query(Bundle).all():
        order = json.loads(bundle.raw_json)
        # Only used here to know, per machine_name, whether every one of its
        # file variants in *this* bundle has a completed Download row — the
        # rest of this loop still reads the raw subproducts list directly
        # (cheaper, and all it ever needed before this).
        by_machine_name: dict[str, list] = {}
        for item in parse_bundle(bundle.gamekey, order).downloads:
            by_machine_name.setdefault(item.machine_name, []).append(item)
        tracked = tracked_by_gamekey.get(bundle.gamekey, {})

        for sp in order.get("subproducts") or []:
            machine_name = sp.get("machine_name") or ""
            if not machine_name:
                continue  # not observed in practice (0/14834), but don't crash if it ever happens
            entry = items.setdefault(machine_name, {"item_name": sp.get("human_name") or machine_name, "bundles": []})
            variants = by_machine_name.get(machine_name, [])
            downloaded = bool(variants) and all(
                tracked.get((v.item_name, v.original_filename)) == STATUS_COMPLETED for v in variants
            )
            entry["bundles"].append(
                {
                    "gamekey": bundle.gamekey,
                    "bundle_name": bundle.name,
                    "purchased_at": bundle.purchased_at,
                    "amount_spent": bundle.amount_spent,
                    "item_count": bundle.subproduct_count,
                    "downloaded": downloaded,
                }
            )
    _catalog_cache["key"] = key
    _catalog_cache["items"] = items
    return items


def avg_item_value(bundles: list[dict]) -> float | None:
    """Effective per-item price: each bundle's total purchase price spread evenly
    across every item it contained, averaged across all bundles an item appeared
    in. A closer estimate of what a (possibly duplicate) item actually cost than
    Humble's own storefront MSRP, which reflects the current for-sale price, not
    what this account paid for it.
    """
    per_bundle = [b["amount_spent"] / b["item_count"] for b in bundles if b.get("item_count")]
    if not per_bundle:
        return None
    return sum(per_bundle) / len(per_bundle)
