"""Custom /docs and /redoc — FastAPI's own defaults (main.py disables them via
docs_url=None, redoc_url=None) don't have any dark mode support, and the user
asked for these to follow the same theme signal as the rest of the app
(localStorage "theme", falling back to prefers-color-scheme — see
static/js/theme-toggle.js) rather than being permanently stuck light.

Neither doc viewer is server-side theme-aware, so both get a small injected
script that decides light/dark client-side, using the exact same precedence
theme-toggle.js already uses, then applies it before the viewer's own bundle
script runs. Still behind the normal session-cookie gate — this router's
routes aren't in deps.py's public-path allowlist, same as the routes they
replace.
"""

import json

from fastapi import APIRouter
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse

router = APIRouter()

_THEME_DETECT_JS = """
function unbundleDocsTheme() {
  var saved = localStorage.getItem("theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
"""

# Swagger UI has no first-class theme API — this is the well-known filter-invert
# trick (flip everything, then flip back anything that should keep its own real
# colors: photos, syntax-highlighted code) rather than overriding every one of
# Swagger UI's internal color rules by hand, which would be far more fragile
# across versions.
#
# Built with plain concatenation, not %-formatting or .format()/f-strings —
# both would misparse the CSS's own "%" (88%, 100%) and "{"/"}" characters as
# format specifiers.
_SWAGGER_DARK_CSS = (
    "\n<style>\n"
    '  [data-theme="dark"] body { background: #17150f; }\n'
    '  [data-theme="dark"] .swagger-ui { filter: invert(88%) hue-rotate(180deg); }\n'
    '  [data-theme="dark"] .swagger-ui .microlight,\n'
    '  [data-theme="dark"] .swagger-ui img { filter: invert(100%) hue-rotate(180deg); }\n'
    "</style>\n"
    "<script>(function () { document.documentElement.dataset.theme = ("
    + _THEME_DETECT_JS.strip()
    + ")(); })();</script>\n"
)

# ReDoc does have a first-class theme object — matched here to app.css's dark
# tokens by value (this static page can't reach the app's CSS custom
# properties), not shared automatically, so a future palette change means
# updating both places.
_REDOC_DARK_THEME = {
    "colors": {
        "primary": {"main": "#5ea3d9"},
        "text": {"primary": "#f1ede4", "secondary": "#a89f8e"},
        "border": {"dark": "#3a352a", "light": "#3a352a"},
        "http": {"get": "#3f7a4d", "post": "#2f6da1", "put": "#e0b258", "delete": "#e08268"},
    },
    "sidebar": {"backgroundColor": "#211e17", "textColor": "#f1ede4"},
    "rightPanel": {"backgroundColor": "#17150f", "textColor": "#f1ede4"},
}


@router.get("/docs", include_in_schema=False, response_class=HTMLResponse)
def custom_swagger_ui_html() -> HTMLResponse:
    resp = get_swagger_ui_html(openapi_url="/openapi.json", title="Unbundle — Swagger UI")
    html = resp.body.decode("utf-8").replace("</head>", _SWAGGER_DARK_CSS + "</head>")
    return HTMLResponse(html)


@router.get("/redoc", include_in_schema=False, response_class=HTMLResponse)
def custom_redoc_html() -> HTMLResponse:
    resp = get_redoc_html(openapi_url="/openapi.json", title="Unbundle — ReDoc")
    # json.dumps() twice: once to produce the JSON string the "theme" attribute
    # itself expects, again to safely embed that string as a JS string literal
    # (JSON string syntax is a strict subset of JS string syntax, so this is
    # always valid regardless of content — no manual quoting/escaping).
    theme_json = json.dumps(_REDOC_DARK_THEME)
    theme_js_literal = json.dumps(theme_json)
    theme_script = f"""
<script>
{_THEME_DETECT_JS}
if (unbundleDocsTheme() === "dark") {{
  document.body.style.background = "#17150f";
  var el = document.querySelector("redoc");
  if (el) el.setAttribute("theme", {theme_js_literal});
}}
</script>
"""
    # Inserted right before the ReDoc bundle script, not on DOMContentLoaded —
    # the <redoc> element above it is already parsed and in the DOM by the
    # time this point in the HTML is reached, and script tags run in document
    # order, so the theme attribute is set before the bundle's own auto-init
    # ever reads it.
    marker = '<script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js">'
    html = resp.body.decode("utf-8").replace(marker, theme_script + marker)
    return HTMLResponse(html)
