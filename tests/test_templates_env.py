from app.templates_env import human_size


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
