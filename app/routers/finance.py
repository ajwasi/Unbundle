"""Financial dashboard: purchases and amounts over time, filterable by year,
month, and category (Humble's own bundle/storefront/subscription* types —
see models/bundle.py's docstring). All aggregation happens in SQL against the
already-persisted Bundle.purchased_at/amount_spent/category columns (added
2026-09-06) — no per-request JSON parsing needed, unlike the Catalog page.

Chart granularity is adaptive: with no specific year chosen, the time-series
chart groups by year (a manageable number of bars even across a long
purchase history); picking a year drills into month-by-month bars for that
year. A `month` filter (with or without a year) narrows *which* purchases
count everywhere (table, both charts) rather than controlling chart shape —
e.g. month=12 with no year answers "how much do I spend each December,
year over year" as a yearly-grouped chart of December-only purchases.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models.bundle import Bundle
from app.models.tag import BundleTag, Tag
from app.templates_env import format_category, templates

router = APIRouter(prefix="/finance")

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _apply_filters(query, year: str, month: str, category: str, tag_id: int | None):
    if year:
        query = query.filter(func.strftime("%Y", Bundle.purchased_at) == year)
    if month:
        query = query.filter(func.strftime("%m", Bundle.purchased_at) == month)
    if category:
        query = query.filter(Bundle.category == category)
    if tag_id is not None:
        query = query.join(BundleTag, BundleTag.gamekey == Bundle.gamekey).filter(BundleTag.tag_id == tag_id)
    return query


@router.get("", response_class=HTMLResponse)
def finance_page(
    request: Request,
    year: str = "",
    month: str = "",
    category: str = "",
    tag_id: str = "",
    db: Session = Depends(get_db),
):
    # str, not int | None: the "All tags" <select> submits an empty string,
    # which FastAPI can't coerce to int and would 422 on.
    tag_id_val = int(tag_id) if tag_id.isdigit() else None
    base = _apply_filters(db.query(Bundle), year, month, category, tag_id_val)
    rows = base.order_by(Bundle.purchased_at.desc()).all()
    filtered_total = sum(b.amount_spent for b in rows)

    granularity = "month" if year else "year"
    period_expr = func.strftime("%Y-%m" if granularity == "month" else "%Y", Bundle.purchased_at)
    period_rows = (
        _apply_filters(db.query(period_expr, func.sum(Bundle.amount_spent), func.count(Bundle.gamekey)), year, month, category, tag_id_val)
        .group_by(period_expr)
        .order_by(period_expr)
        .all()
    )
    chart_labels = [p or "unknown" for p, _, _ in period_rows]
    chart_values = [round(total or 0.0, 2) for _, total, _ in period_rows]

    category_rows = (
        _apply_filters(db.query(Bundle.category, func.sum(Bundle.amount_spent)), year, month, category, tag_id_val)
        .group_by(Bundle.category)
        .order_by(Bundle.category)
        .all()
    )
    category_labels = [format_category(c) if c else "(none)" for c, _ in category_rows]
    category_values = [round(total or 0.0, 2) for _, total in category_rows]

    all_years = [
        row[0] for row in db.query(func.strftime("%Y", Bundle.purchased_at)).distinct().order_by(func.strftime("%Y", Bundle.purchased_at).desc()).all() if row[0]
    ]
    all_categories = [row[0] for row in db.query(Bundle.category).distinct().order_by(Bundle.category).all() if row[0]]

    context = {
        "rows": rows,
        "year": year,
        "month": month,
        "month_name": _MONTH_NAMES[int(month) - 1] if month else "",
        "category": category,
        "tag_id": tag_id_val,
        "all_years": all_years,
        "all_categories": all_categories,
        "all_tags": db.query(Tag).order_by(Tag.name).all(),
        "months": list(enumerate(_MONTH_NAMES, start=1)),
        "filtered_total": filtered_total,
        "filtered_count": len(rows),
        "granularity": granularity,
        "chart_labels": chart_labels,
        "chart_values": chart_values,
        "category_labels": category_labels,
        "category_values": category_values,
    }
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "finance/_results.html", context)
    return templates.TemplateResponse(request, "finance/list.html", context)
