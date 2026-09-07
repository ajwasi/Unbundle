"""Catalog: every distinct item across every purchased bundle, flattened, with
duplicate-purchase detection. Keyed by subproduct `machine_name` — confirmed
present on all 14,834 subproduct rows across a real 550-bundle library
(2026-09-06), so it's a reliable cross-bundle identity key with no need for
fuzzy human_name matching (titles could vary slightly between bundle
listings; machine_name is Humble's own stable slug).

Computed fresh from Bundle.raw_json on every request rather than a persisted
table — confirmed cheap enough not to matter (0.13s to parse+group all 550
bundles' JSON on this machine), consistent with how bundle_detail already
re-derives its view from raw_json rather than caching a denormalized copy.
Price/date per bundle come from the already-persisted Bundle.amount_spent/
purchased_at columns rather than re-parsing those two fields out of raw_json.
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models.bundle import Bundle
from app.templates_env import templates

router = APIRouter(prefix="/catalog")


def _build_catalog(db: Session) -> dict[str, dict]:
    items: dict[str, dict] = {}
    for bundle in db.query(Bundle).all():
        order = json.loads(bundle.raw_json)
        for sp in order.get("subproducts") or []:
            machine_name = sp.get("machine_name") or ""
            if not machine_name:
                continue  # not observed in practice (0/14834), but don't crash if it ever happens
            entry = items.setdefault(machine_name, {"item_name": sp.get("human_name") or machine_name, "bundles": []})
            entry["bundles"].append(
                {
                    "gamekey": bundle.gamekey,
                    "bundle_name": bundle.name,
                    "purchased_at": bundle.purchased_at,
                    "amount_spent": bundle.amount_spent,
                    "item_count": bundle.subproduct_count,
                }
            )
    return items


def _avg_item_value(bundles: list[dict]) -> float | None:
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


def _rows_from_catalog(items: dict[str, dict]) -> list[dict]:
    rows = []
    for key, entry in items.items():
        dates = [b["purchased_at"] for b in entry["bundles"] if b["purchased_at"]]
        rows.append(
            {
                "key": key,
                "item_name": entry["item_name"],
                "count": len(entry["bundles"]),
                "bundles": entry["bundles"],
                "first_purchased": min(dates) if dates else None,
            }
        )
    return rows


@router.get("", response_class=HTMLResponse)
def catalog_page(
    request: Request,
    q: str = "",
    dupes_only: bool = False,
    sort: str = "name",
    dir: str = "asc",
    db: Session = Depends(get_db),
):
    items = _build_catalog(db)
    rows = _rows_from_catalog(items)

    if q:
        q_lower = q.lower()
        rows = [r for r in rows if q_lower in r["item_name"].lower()]
    if dupes_only:
        rows = [r for r in rows if r["count"] > 1]

    reverse = dir == "desc"
    if sort == "count":
        rows.sort(key=lambda r: r["count"], reverse=reverse)
    elif sort == "purchased":
        # datetime.min fallback, not "" — first_purchased is a datetime when present
        # (never observed missing, but a str/datetime mix would crash this sort).
        rows.sort(key=lambda r: r["first_purchased"] or datetime.min, reverse=reverse)
    else:
        rows.sort(key=lambda r: r["item_name"].casefold(), reverse=reverse)

    context = {
        "rows": rows,
        "q": q,
        "dupes_only": dupes_only,
        "sort": sort,
        "dir": dir,
        "total_items": len(items),
        "total_dupes": sum(1 for e in items.values() if len(e["bundles"]) > 1),
        "grand_total_spent": db.query(func.sum(Bundle.amount_spent)).scalar() or 0.0,
    }
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "catalog/_table.html", context)
    return templates.TemplateResponse(request, "catalog/list.html", context)


@router.get("/item/{machine_name}/bundles", response_class=HTMLResponse)
def item_bundles(request: Request, machine_name: str, db: Session = Depends(get_db)):
    items = _build_catalog(db)
    entry = items.get(machine_name, {"item_name": machine_name, "bundles": []})
    bundles = sorted(entry["bundles"], key=lambda b: b["purchased_at"] or datetime.min)
    return templates.TemplateResponse(
        request,
        "catalog/_bundles_modal_content.html",
        {"item_name": entry["item_name"], "bundles": bundles, "avg_item_value": _avg_item_value(bundles)},
    )
