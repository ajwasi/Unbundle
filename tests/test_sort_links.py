"""The sortable-table header link is one shared macro (_macros.html) wrapped
per page; each page must still build its own filtered base URL, flip the
direction on a repeat click, and target its own table.

Renders each table partial directly: the pages themselves show a "not
connected" screen until a credential exists, which isn't what's under test.
"""

import pytest
from jinja2 import ChainableUndefined

from app.templates_env import templates

# These partials render a whole page's worth of other context (totals, request,
# csrf...) that has nothing to do with the header link; tolerate it being absent.
_env = templates.env.overlay(undefined=ChainableUndefined)

# (template, active sort column, filters, expected hx-get prefix, hx-target)
CASES = [
    ("chirp/_books_table.html", "title", {"q": "a b"}, "/chirp?q=a%20b", "#chirp-books-table"),
    ("gog/_games_table.html", "title", {"title": "x&y", "content_type": "game"}, "/gog?title=x%26y&content_type=game", "#gog-games-table"),
    ("steam/_games_table.html", "name", {"name": "zz", "playtime_min": None, "playtime_max": 5}, "/steam?name=zz&playtime_min=&playtime_max=5", "#steam-games-table"),
    ("bundles/_table.html", "name", {"q": "Q", "category": "", "min_items": 3, "tag_id": None, "redeemed": ""}, "/bundles?q=Q&category=&min_items=3&tag_id=&redeemed=", "#bundle-table"),
    ("catalog/_table.html", "name", {"q": "Q", "dupes_only": True, "tag_id": 7}, "/catalog?q=Q&dupes_only=true&tag_id=7", "#catalog-table"),
    ("audible/_books_table.html", "title", {"title": "T", "author": "A"}, "/audible?title=T&author=A&", "#audible-books-table"),
]


@pytest.mark.parametrize("template,column,filters,prefix,target", CASES)
def test_sort_header_link_keeps_filters_target_and_flips_direction(template, column, filters, prefix, target):
    context = {"sort": column, "dir": "asc", "books": [], "games": [], "bundles": [], "items": [], "rows": [], "filtered_total_spent": 0, **filters}
    html = _env.get_template(template).render(context)

    start = html.index(f'hx-get="{prefix}')
    link = html[start : html.index("</a>", start)]
    # The active column's link now sorts descending, with the filters carried along.
    assert f"&sort={column}&dir=desc\"" in link
    assert f'hx-target="{target}" hx-swap="outerHTML"' in link
    assert "&#9650;" in link  # ascending arrow on the active column
