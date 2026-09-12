from datetime import datetime, timedelta

from app.templates_env import format_category, human_size, time_since


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


def test_time_since_handles_none():
    assert time_since(None) == "never"


def test_time_since_just_now():
    assert time_since(datetime.utcnow() - timedelta(seconds=30)) == "just now"


def test_time_since_minutes():
    assert time_since(datetime.utcnow() - timedelta(minutes=5)) == "5 minutes ago"


def test_time_since_singular_minute():
    assert time_since(datetime.utcnow() - timedelta(minutes=1)) == "1 minute ago"


def test_time_since_hours():
    assert time_since(datetime.utcnow() - timedelta(hours=4)) == "4 hours ago"


def test_time_since_singular_hour():
    assert time_since(datetime.utcnow() - timedelta(hours=1)) == "1 hour ago"


def test_time_since_days():
    assert time_since(datetime.utcnow() - timedelta(days=3)) == "3 days ago"


def test_time_since_singular_day():
    assert time_since(datetime.utcnow() - timedelta(days=1)) == "1 day ago"
