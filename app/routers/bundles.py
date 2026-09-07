import json
from collections import Counter

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import asc, desc, func
from sqlalchemy.orm import Session

from app.connectors.humble_connector import order_page_url, parse_bundle
from app.deps import get_db
from app.downloads import worker
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.download import STATUS_COMPLETED as FILE_COMPLETED, STATUS_FAILED as FILE_FAILED, Download
from app.models.download_job import STATUS_FAILED as JOB_FAILED, STATUS_COMPLETED as JOB_COMPLETED
from app.models.sync_run import STATUS_FAILED, STATUS_SUCCESS
from app.sync import refresh
from app.templates_env import templates

router = APIRouter(prefix="/bundles")

_SORT_COLUMNS = {
    "name": Bundle.name,
    "items": Bundle.subproduct_count,
    "category": Bundle.category,
    "purchased": Bundle.purchased_at,
    "price": Bundle.amount_spent,
}


def _apply_filters(query, q: str, category: str, min_items: int | None):
    if q:
        query = query.filter(Bundle.name.ilike(f"%{q}%"))
    if category:
        query = query.filter(Bundle.category == category)
    if min_items is not None:
        query = query.filter(Bundle.subproduct_count >= min_items)
    return query


def _apply_sort(query, sort: str, direction: str):
    column = _SORT_COLUMNS.get(sort, Bundle.name)
    return query.order_by(desc(column) if direction == "desc" else asc(column))


def _category_breakdown(db: Session) -> list[dict]:
    """Independent of the current search/filter — a stable spending overview
    across the whole library, distinct from the table footer's filtered
    subtotal."""
    rows = (
        db.query(Bundle.category, func.count(Bundle.gamekey), func.sum(Bundle.amount_spent))
        .group_by(Bundle.category)
        .order_by(Bundle.category)
        .all()
    )
    return [{"category": cat or "(none)", "count": cnt, "total": total or 0.0} for cat, cnt, total in rows]


@router.get("", response_class=HTMLResponse)
def list_bundles(
    request: Request,
    q: str = "",
    category: str = "",
    min_items: int | None = None,
    sort: str = "name",
    dir: str = "asc",
    db: Session = Depends(get_db),
):
    query = _apply_filters(db.query(Bundle), q, category, min_items)
    query = _apply_sort(query, sort, dir)
    bundles = query.all()

    categories = [row[0] for row in db.query(Bundle.category).distinct().order_by(Bundle.category).all() if row[0]]

    context = {
        "bundles": bundles,
        "q": q,
        "category": category,
        "min_items": min_items,
        "sort": sort,
        "dir": dir,
        "categories": categories,
        "total_count": db.query(Bundle).count(),
        "filtered_total_spent": sum(b.amount_spent for b in bundles),
        "category_breakdown": _category_breakdown(db),
        "grand_total_spent": db.query(func.sum(Bundle.amount_spent)).scalar() or 0.0,
    }
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "bundles/_table.html", context)
    return templates.TemplateResponse(request, "bundles/list.html", context)


@router.post("/refresh", response_class=HTMLResponse)
async def refresh_bundles(request: Request):
    try:
        await refresh.start_refresh()
    except RuntimeError:
        pass  # already running — the polling view below will just keep polling
    return templates.TemplateResponse(request, "bundles/_refresh_status.html", {"running": True})


@router.get("/refresh/status", response_class=HTMLResponse)
def refresh_status(request: Request, db: Session = Depends(get_db)):
    if refresh.is_refresh_running():
        return templates.TemplateResponse(request, "bundles/_refresh_status.html", {"running": True})

    run = refresh.latest_run(db)
    message = None
    if run and run.status == STATUS_SUCCESS:
        message = f"Refreshed {run.bundle_count} bundle(s)."
    elif run and run.status == STATUS_FAILED:
        message = f"Refresh failed: {run.error_message}"
    return templates.TemplateResponse(request, "bundles/_refresh_status.html", {"running": False, "message": message})


def _format_key(fmt: str) -> str:
    """Case/whitespace-insensitive grouping key. Confirmed against the real
    library (2026-09-06): 10 bundles mix casing variants of the same format
    within one bundle (e.g. 'Zip' and 'ZIP' both present), which fragmented
    the column-per-format table into confusing near-duplicates.
    """
    return (fmt or "").strip().casefold()


def _canonical_format_labels(downloads: list) -> dict[str, str]:
    """key -> display label (the most common exact casing seen for that key,
    within this bundle)."""
    groups: dict[str, Counter] = {}
    for item in downloads:
        fmt = (item.file_format or "").strip()
        if fmt:
            groups.setdefault(_format_key(fmt), Counter())[fmt] += 1
    return {key: counter.most_common(1)[0][0] for key, counter in groups.items()}


def _expand_format_keys(downloads: list, keys: list[str]) -> list[str]:
    """Reverse-maps submitted canonical keys back to every real raw format
    string in this bundle that belongs to them, so a bulk "download this
    format" action still passes humble-cli `-f` for each actual variant
    (e.g. both 'Zip' and 'ZIP') rather than a merged label it may not
    recognize — humble-cli's own case-sensitivity here hasn't been verified,
    so this sidesteps needing it to be case-insensitive at all.
    """
    keyset = set(keys)
    raws = {(item.file_format or "").strip() for item in downloads if _format_key(item.file_format) in keyset}
    return sorted(raws)


def _group_items(downloads: list, tracked: dict[tuple, Download]) -> list[dict]:
    """Groups the flat per-file NormalizedDownloadItem list by subproduct_index
    for display+selection — humble-cli's `-i` selects a whole subproduct at
    once (confirmed against the real binary), so bulk selection operates at
    that granularity. `by_format` keys each item's variants by the
    case-insensitive format key (see `_format_key`), storing the item's own
    exact `raw_format` string alongside so per-cell downloads always pass
    humble-cli the real string, never a merged display label.

    `summary` reasons about the whole set of format-variant statuses for this
    item, not just the first one — a naive "show statuses[0]" was found (real
    account, multi-format item) to render a completed variant's status text
    inside an error-styled badge whenever a *different* variant of the same
    item hadn't been downloaded yet.
    """
    groups: dict[int, dict] = {}
    for item in downloads:
        g = groups.setdefault(
            item.subproduct_index,
            {"subproduct_index": item.subproduct_index, "item_name": item.item_name, "by_format": {}, "total_size_bytes": 0},
        )
        fmt = (item.file_format or "").strip()
        key = _format_key(fmt) if fmt else "file"
        row = tracked.get((item.item_name, item.original_filename))
        existing = g["by_format"].get(key)
        if existing is None:
            g["by_format"][key] = {
                "raw_format": fmt,
                "size_bytes": item.expected_size_bytes,
                "filename": item.original_filename,
                "status": row.status if row else None,
            }
        else:
            # Same item with two raw variants of "the same" format — not observed in
            # practice, but merge defensively rather than silently drop one.
            existing["size_bytes"] += item.expected_size_bytes
        g["total_size_bytes"] += item.expected_size_bytes

    ordered = sorted(groups.values(), key=lambda g: g["subproduct_index"])
    for g in ordered:
        statuses = [f["status"] for f in g["by_format"].values()]
        tracked_statuses = [s for s in statuses if s is not None]
        if not tracked_statuses:
            g["summary"] = "none"
        elif any(s == FILE_FAILED for s in tracked_statuses):
            g["summary"] = "failed"
        elif all(s == FILE_COMPLETED for s in statuses):
            g["summary"] = "downloaded"
        else:
            g["summary"] = "partial"
    return ordered


def _compute_footer(item_groups: list[dict], format_keys: list[str]) -> dict:
    format_totals = {key: 0 for key in format_keys}
    for g in item_groups:
        for key, info in g["by_format"].items():
            if key in format_totals:
                format_totals[key] += info["size_bytes"]
    return {
        "item_count": len(item_groups),
        "format_totals": format_totals,
        "grand_total": sum(g["total_size_bytes"] for g in item_groups),
    }


def _build_item_context(gamekey: str, bundle: Bundle, db: Session) -> dict:
    normalized = parse_bundle(gamekey, json.loads(bundle.raw_json))
    tracked = {
        (d.item_name, d.original_filename): d for d in db.query(Download).filter(Download.gamekey == gamekey).all()
    }
    item_groups = _group_items(normalized.downloads, tracked)
    canon_labels = _canonical_format_labels(normalized.downloads)
    all_formats = sorted(canon_labels.items(), key=lambda kv: kv[1])  # [(key, label), ...] by label
    footer = _compute_footer(item_groups, [key for key, _ in all_formats])
    avg_item_price = bundle.amount_spent / len(item_groups) if item_groups else None
    return {
        "bundle": bundle,
        "item_groups": item_groups,
        "all_formats": all_formats,
        "footer": footer,
        "avg_item_price": avg_item_price,
    }


def _entitlement_rows(gamekey: str, entitlements: list[BundleEntitlement]) -> list[dict]:
    """key_type isn't its own persisted column (raw_json already has it, same
    "re-derive at render time" choice made for downloads) — used to pick which
    of steam_owned/gog_owned (if either) is meaningful for this row, so a
    single generic "Ownership" column can show "Steam: No" or "GOG: No"
    without a bare "No" misleadingly implying an Origin/Uplay/etc. key was
    ever actually checked against anything.
    """
    rows = []
    for e in entitlements:
        try:
            key_type = (json.loads(e.raw_json) if e.raw_json else {}).get("key_type", "")
        except (ValueError, TypeError):
            key_type = ""

        ownership_platform = None
        owned = None
        if key_type == "steam":
            ownership_platform, owned = "Steam", e.steam_owned
        elif key_type == "gog":
            ownership_platform, owned = "GOG", e.gog_owned

        rows.append(
            {
                "key_name": e.key_name,
                "platform": key_type,
                "redeemed_on_humble": e.redeemed_on_humble,
                "ownership_platform": ownership_platform,
                "owned": owned,
                "redeem_url": order_page_url(gamekey),
            }
        )
    return rows


@router.get("/{gamekey}", response_class=HTMLResponse)
def bundle_detail(request: Request, gamekey: str, db: Session = Depends(get_db)):
    bundle = db.get(Bundle, gamekey)
    if bundle is None:
        return templates.TemplateResponse(request, "bundles/not_found.html", {"gamekey": gamekey}, status_code=404)

    entitlements = (
        db.query(BundleEntitlement).filter(BundleEntitlement.gamekey == gamekey).order_by(BundleEntitlement.key_name).all()
    )
    context = _build_item_context(gamekey, bundle, db)
    context["entitlements"] = _entitlement_rows(gamekey, entitlements)

    return templates.TemplateResponse(request, "bundles/detail.html", context)


@router.post("/{gamekey}/download", response_class=HTMLResponse)
async def trigger_download(
    request: Request,
    gamekey: str,
    items: list[int] = Form(default=[]),
    formats: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    bundle = db.get(Bundle, gamekey)
    if bundle is None:
        return templates.TemplateResponse(request, "bundles/not_found.html", {"gamekey": gamekey}, status_code=404)

    expanded_formats = None
    if formats:
        normalized = parse_bundle(gamekey, json.loads(bundle.raw_json))
        expanded_formats = _expand_format_keys(normalized.downloads, formats)

    try:
        await worker.start_download(gamekey, bundle.name, items or None, expanded_formats)
    except RuntimeError:
        pass  # already running — the polling view below will just keep polling
    return templates.TemplateResponse(request, "bundles/_download_status.html", {"gamekey": gamekey, "running": True})


@router.post("/{gamekey}/download/item/{subproduct_index}", response_class=HTMLResponse)
async def trigger_item_download(
    request: Request,
    gamekey: str,
    subproduct_index: int,
    format: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Either one (item, format) cell's download button (`format` given — confirmed
    `-i <idx> -f <FORMAT>` downloads exactly that combination against the real
    binary; `format` is always the item's own exact raw format string, see
    _group_items' raw_format, never a merged display label) or the per-row
    "download this whole item" button (`format` omitted — just `-i <idx>`,
    every format this item has)."""
    bundle = db.get(Bundle, gamekey)
    if bundle is None:
        return templates.TemplateResponse(request, "bundles/not_found.html", {"gamekey": gamekey}, status_code=404)

    try:
        await worker.start_download(gamekey, bundle.name, [subproduct_index], [format] if format else None)
    except RuntimeError:
        pass  # already running — the polling view below will just keep polling
    return templates.TemplateResponse(request, "bundles/_download_status.html", {"gamekey": gamekey, "running": True})


@router.get("/{gamekey}/download/status", response_class=HTMLResponse)
def download_status(request: Request, gamekey: str, db: Session = Depends(get_db)):
    if worker.is_download_running():
        return templates.TemplateResponse(request, "bundles/_download_status.html", {"gamekey": gamekey, "running": True})

    job = worker.latest_job_for_bundle(db, gamekey)
    message = None
    if job and job.status == JOB_COMPLETED:
        message = "Download complete."
    elif job and job.status == JOB_FAILED:
        message = f"Download failed: {job.error_message}"

    context = {"gamekey": gamekey, "running": False, "message": message}
    if message:
        bundle = db.get(Bundle, gamekey)
        if bundle is not None:
            context.update(_build_item_context(gamekey, bundle, db))
    return templates.TemplateResponse(request, "bundles/_download_status.html", context)
