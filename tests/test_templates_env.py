from app.templates_env import format_category, human_size


def test_human_size_bytes():
    assert human_size(500) == "500 B"


def test_human_size_kb():
    assert human_size(2048) == "2.0 KB"


def test_human_size_mb():
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


def test_human_size_gb():
    assert human_size(3 * 1024 * 1024 * 1024) == "3.0 GB"


def test_human_size_handles_none():
    assert human_size(None) == "0 B"


def test_human_size_handles_zero():
    assert human_size(0) == "0 B"


def test_format_category_known_values():
    # These are Humble's own real values (confirmed against the live library) —
    # single concatenated words, no delimiter to split algorithmically.
    assert format_category("bundle") == "Bundle"
    assert format_category("storefront") == "Storefront"
    assert format_category("subscriptioncontent") == "Subscription Content"
    assert format_category("subscriptionplan") == "Subscription Plan"
    assert format_category("widget") == "Widget"


def test_format_category_passes_through_the_none_sentinel():
    # routers/bundles.py's _category_breakdown() substitutes this exact string
    # for an uncategorized bundle before the template ever sees it.
    assert format_category("(none)") == "(none)"


def test_format_category_falls_back_to_title_case_for_unknown_values():
    assert format_category("some_new_category") == "Some New Category"


def test_format_category_handles_empty_string():
    assert format_category("") == ""
