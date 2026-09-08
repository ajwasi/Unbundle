from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.version import get_version, is_update_available

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# app_version is computed once (the process's own version never changes at runtime);
# is_update_available is registered as the function itself, not its current result —
# its answer changes as the background check loop re-runs, so it must be called
# fresh on every render, not baked in at import time.
templates.env.globals["app_version"] = get_version()
templates.env.globals["is_update_available"] = is_update_available


def human_size(num_bytes: int) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


templates.env.filters["human_size"] = human_size
