"""The badge vocabulary (_macros.html): one meaning per kind, and the pages
the design review caught reusing one class for unrelated meanings stay fixed."""

import re
from pathlib import Path

import pytest

from app.templates_env import templates

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _render(source: str) -> str:
    return templates.env.from_string('{% from "_macros.html" import badge %}' + source).render()


@pytest.mark.parametrize(
    "kind,css",
    [("ok", "badge-ok"), ("error", "badge-error"), ("progress", "badge-progress"),
     ("absent", "badge-absent"), ("info", "badge-info"), ("off", "badge-not-configured")],
)
def test_each_kind_maps_to_its_own_class(kind, css):
    assert _render(f'{{{{ badge("{kind}", "Label") }}}}') == f'<span class="badge {css}">Label</span>'


def test_a_tip_becomes_the_hover_title_and_is_escaped():
    html = _render('{{ badge("absent", "Not found", "No <b>Plex</b> here") }}')
    assert 'class="badge badge-absent tip"' in html
    assert 'title="No &lt;b&gt;Plex&lt;/b&gt; here"' in html


def test_an_unknown_kind_fails_loudly_instead_of_rendering_unstyled():
    with pytest.raises(ValueError, match="Unknown badge kind: pending"):
        _render('{{ badge("pending", "x") }}')


# Labels whose meaning the design review found being spelled with the wrong class.
_GUARDED = r"(Plus Catalog|Experimental|Missing|Not found|Downloading|Queued|Running|No longer listed)"


def test_guarded_labels_are_never_hand_written_as_badges():
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        for number, line in enumerate(path.read_text(encoding="utf8").splitlines(), 1):
            if re.search(r'class="badge[^"]*"[^>]*>\s*' + _GUARDED, line):
                offenders.append(f"{path.relative_to(TEMPLATES)}:{number}")
    assert not offenders, "use badge(kind, label) from _macros.html: " + ", ".join(offenders)


def test_every_amazon_music_checkbox_has_an_accessible_name():
    unnamed = []
    for path in (TEMPLATES / "amazon_music").glob("*.html"):
        for tag in re.findall(r"<input[^>]*type=\"checkbox\"[^>]*>", path.read_text(encoding="utf8")):
            # A visible <label for=...> via id is also fine.
            if "aria-label" not in tag and " id=" not in tag:
                unnamed.append(f"{path.name}: {tag[:70]}")
    assert not unnamed, unnamed


def test_download_history_table_scrolls_instead_of_overflowing():
    html = (TEMPLATES / "downloads" / "_history.html").read_text(encoding="utf8")
    assert html.index('class="table-scroll"') < html.index("<table>")
