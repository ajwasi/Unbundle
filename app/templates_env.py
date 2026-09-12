from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.version import get_version, is_update_available, latest_tag

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# app_version is computed once (the process's own version never changes at runtime);
# is_update_available/latest_tag are registered as the functions themselves, not
# their current results — both change as the background check loop re-runs, so
# they must be called fresh on every render, not baked in at import time.
templates.env.globals["app_version"] = get_version()
templates.env.globals["is_update_available"] = is_update_available
templates.env.globals["latest_version_tag"] = latest_tag


def human_size(num_bytes: int) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


templates.env.filters["human_size"] = human_size

# Humble's own category values are single concatenated words ("subscriptioncontent"),
# not underscore-separated like the credential-status values elsewhere in this app
# (those already render fine via a plain `| replace('_', ' ')`) — no algorithmic way
# to insert word boundaries into a concatenated string, so this is a real mapping.
# Also used directly from app/routers/finance.py for the spending-by-category chart's
# labels, not just as a Jinja filter, so it lives here rather than duplicated.
_CATEGORY_LABELS = {
    "bundle": "Bundle",
    "storefront": "Storefront",
    "subscriptioncontent": "Subscription Content",
    "subscriptionplan": "Subscription Plan",
    "widget": "Widget",
    # Not a real category — routers/bundles.py's _category_breakdown() already
    # substitutes this exact sentinel for an uncategorized bundle before the
    # template ever sees it, so it needs its own passthrough here too.
    "(none)": "(none)",
}


def format_category(value: str) -> str:
    if not value:
        return value
    return _CATEGORY_LABELS.get(value, value.replace("_", " ").title())


templates.env.filters["format_category"] = format_category
