"""Shared plumbing for this app's search/sort list pages (Bundles, Catalog,
Steam, GOG, Audible) — only the parts genuinely identical across all of
them. Each page's own filter fields, sort-column sets, and templates stay
local; forcing a bigger one-size-fits-all "list page" abstraction here
would cost more in indirection than the near-duplicate copies it'd replace
(see gog.py's sort-by-type tiebreak and catalog.py's in-memory sort for two
call sites that genuinely don't fit this shape).
"""

from sqlalchemy import asc, desc


def render_list_or_partial(request, templates, full_template: str, partial_template: str, context: dict):
    """A full page load renders full_template; an htmx filter/sort request
    (targeting just the table) renders partial_template instead — same
    context either way. Used identically by every list page's GET route.
    """
    template = partial_template if request.headers.get("HX-Request") == "true" else full_template
    return templates.TemplateResponse(request, template, context)


def sorted_query(query, sort_columns: dict, sort: str, dir: str, default_column):
    """Applies asc/desc ordering by a sort key that maps to a real column via
    sort_columns, falling back to default_column for an unrecognized key."""
    column = sort_columns.get(sort, default_column)
    return query.order_by(desc(column) if dir == "desc" else asc(column))
