from app.connectors.base import NormalizedDownloadItem
from app.models.download import STATUS_COMPLETED, STATUS_FAILED, Download
from app.routers.bundles import (
    _canonical_format_labels,
    _compute_footer,
    _expand_format_keys,
    _format_key,
    _group_items,
)


def _item(item_name="Item", index=1, fmt="EPUB", filename="f.epub", size=100):
    return NormalizedDownloadItem(
        item_name=item_name, subproduct_index=index, file_format=fmt, original_filename=filename,
        source_url=f"https://x/{filename}", expected_size_bytes=size,
    )


def test_format_key_is_casefold_and_stripped():
    assert _format_key(" ZIP ") == "zip"
    assert _format_key("Zip") == "zip"
    assert _format_key("zip") == "zip"


def test_format_key_handles_none_and_empty():
    assert _format_key(None) == ""
    assert _format_key("") == ""


def test_canonical_format_labels_picks_most_common_casing():
    downloads = [_item(fmt="Zip"), _item(fmt="Zip"), _item(fmt="ZIP")]
    labels = _canonical_format_labels(downloads)
    assert labels == {"zip": "Zip"}


def test_canonical_format_labels_ignores_blank_formats():
    downloads = [_item(fmt=""), _item(fmt="EPUB")]
    labels = _canonical_format_labels(downloads)
    assert labels == {"epub": "EPUB"}


def test_expand_format_keys_returns_every_raw_variant_for_a_canonical_key():
    downloads = [_item(fmt="Zip"), _item(fmt="ZIP"), _item(fmt="PDF")]
    result = _expand_format_keys(downloads, ["zip"])
    assert result == ["ZIP", "Zip"]


def test_expand_format_keys_multiple_keys():
    downloads = [_item(fmt="Zip"), _item(fmt="PDF"), _item(fmt="EPUB")]
    result = _expand_format_keys(downloads, ["zip", "pdf"])
    assert result == ["PDF", "Zip"]


def test_group_items_groups_by_subproduct_index():
    downloads = [_item(item_name="A", index=1, fmt="EPUB"), _item(item_name="B", index=2, fmt="PDF")]
    groups = _group_items(downloads, {})
    assert len(groups) == 2
    assert groups[0]["item_name"] == "A"
    assert groups[1]["item_name"] == "B"


def test_group_items_merges_format_variants_case_insensitively():
    downloads = [_item(fmt="Zip", size=100, filename="a.zip"), _item(fmt="ZIP", size=200, filename="a.zip")]
    groups = _group_items(downloads, {})
    assert len(groups) == 1
    assert list(groups[0]["by_format"].keys()) == ["zip"]
    # Merged defensively (not observed in practice, but must not silently drop one).
    assert groups[0]["by_format"]["zip"]["size_bytes"] == 300


def test_group_items_preserves_exact_raw_format_string_per_cell():
    # The stored raw_format must be this item's OWN casing, never the merged label —
    # confirmed against the real CLI that -f is (at least potentially) case-sensitive.
    downloads = [_item(fmt="ZIP")]
    groups = _group_items(downloads, {})
    assert groups[0]["by_format"]["zip"]["raw_format"] == "ZIP"


def test_group_items_total_size_sums_across_formats():
    downloads = [_item(index=1, fmt="EPUB", size=100, filename="a.epub"), _item(index=1, fmt="PDF", size=200, filename="a.pdf")]
    groups = _group_items(downloads, {})
    assert groups[0]["total_size_bytes"] == 300


def test_group_items_summary_none_when_untracked():
    downloads = [_item()]
    groups = _group_items(downloads, {})
    assert groups[0]["summary"] == "none"


def test_group_items_summary_downloaded_when_all_completed():
    d = Download(gamekey="GK", item_name="Item", original_filename="f.epub", status=STATUS_COMPLETED)
    downloads = [_item(item_name="Item", filename="f.epub")]
    tracked = {("Item", "f.epub"): d}
    groups = _group_items(downloads, tracked)
    assert groups[0]["summary"] == "downloaded"


def test_group_items_summary_failed_when_any_failed():
    d1 = Download(gamekey="GK", item_name="Item", original_filename="a.epub", status=STATUS_COMPLETED)
    d2 = Download(gamekey="GK", item_name="Item", original_filename="b.pdf", status=STATUS_FAILED)
    downloads = [
        _item(item_name="Item", fmt="EPUB", filename="a.epub"),
        _item(item_name="Item", fmt="PDF", filename="b.pdf"),
    ]
    tracked = {("Item", "a.epub"): d1, ("Item", "b.pdf"): d2}
    groups = _group_items(downloads, tracked)
    assert groups[0]["summary"] == "failed"


def test_group_items_summary_partial_when_some_untracked_and_none_failed():
    # Real bug this guards against: a multi-format item where only SOME formats
    # were downloaded used to render the tracked status inside an error badge.
    d1 = Download(gamekey="GK", item_name="Item", original_filename="a.epub", status=STATUS_COMPLETED)
    downloads = [
        _item(item_name="Item", fmt="EPUB", filename="a.epub"),
        _item(item_name="Item", fmt="PDF", filename="b.pdf"),
    ]
    tracked = {("Item", "a.epub"): d1}
    groups = _group_items(downloads, tracked)
    assert groups[0]["summary"] == "partial"


def test_compute_footer_sums_item_count_and_format_totals():
    downloads = [
        _item(item_name="A", index=1, fmt="EPUB", size=100, filename="a.epub"),
        _item(item_name="B", index=2, fmt="EPUB", size=50, filename="b.epub"),
        _item(item_name="B", index=2, fmt="PDF", size=25, filename="b.pdf"),
    ]
    groups = _group_items(downloads, {})
    footer = _compute_footer(groups, ["epub", "pdf"])
    assert footer["item_count"] == 2
    assert footer["format_totals"] == {"epub": 150, "pdf": 25}
    assert footer["grand_total"] == 175
