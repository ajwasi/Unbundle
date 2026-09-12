import os
from pathlib import Path

import pytest

from app.downloads.paths import (
    PathTraversalError,
    humanize_filename,
    long_path_safe,
    predict_download_path,
    resolve_within,
    sanitize_dir_name,
)


def test_sanitize_replaces_each_invalid_character():
    assert sanitize_dir_name('a/b\\c?d%e*f:g|h"i<j>k;l=m') == "a b c d e f g h i j k l m"


def test_sanitize_replaces_newline():
    assert sanitize_dir_name("line1\nline2") == "line1 line2"


def test_sanitize_does_not_collapse_resulting_double_spaces():
    # Confirmed against the real binary: "Humble Software REBundle: VEGAS Pro"
    # (": " already has a following space) becomes a double space, not a single
    # one — humble-cli does no whitespace cleanup, so neither can this.
    assert sanitize_dir_name("Humble Software REBundle: VEGAS Pro") == "Humble Software REBundle  VEGAS Pro"


def test_sanitize_leaves_ordinary_names_untouched():
    assert sanitize_dir_name("Ordinary Bundle Name") == "Ordinary Bundle Name"


def test_humanize_filename_replaces_underscores_with_spaces():
    assert humanize_filename("some_book_vol_02.epub") == "some book vol 02.epub"


def test_humanize_filename_leaves_extension_and_ordinary_names_untouched():
    assert humanize_filename("Ordinary Name.epub") == "Ordinary Name.epub"


def test_humanize_filename_is_a_pure_substitution_never_collapsing_distinct_names():
    # Injective by construction (character-for-character swap) — two
    # differently-numbered files can never end up sharing a humanized name.
    assert humanize_filename("book_01.epub") != humanize_filename("book_02.epub")


def test_predict_download_path_builds_three_level_path():
    result = predict_download_path("My: Bundle", "Item/Name", "file.epub")
    assert result == Path("My  Bundle") / "Item Name" / "file.epub"


def test_resolve_within_accepts_a_normal_nested_path(tmp_path):
    result = resolve_within(tmp_path, Path("Bundle") / "Item" / "file.epub")
    assert result == (tmp_path / "Bundle" / "Item" / "file.epub").resolve()


def test_resolve_within_rejects_dotdot_climbing_out_of_root(tmp_path):
    # sanitize_dir_name() strips path separators but ".." contains none, so a
    # bundle/item name of exactly ".." reaches here unchanged from predict_download_path.
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, Path("..") / ".." / "evil.txt")


def test_resolve_within_rejects_dotdot_in_a_middle_segment(tmp_path):
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, Path("Bundle") / ".." / ".." / "evil.txt")


# long_path_safe branches on the *real* os.name rather than an injectable seam,
# and Path() construction itself dispatches on that same global (WindowsPath vs.
# PosixPath) — monkeypatching os.name to fake the other platform was tried and
# rejected: it corrupted pathlib's own dispatch mid-test and broke pytest's own
# failure reporting. So this only exercises whichever branch the CI/dev host
# actually is; both branches do get covered over time (Windows dev machine +
# Linux Docker deployment target), just never both in the same run.
def test_long_path_safe_matches_current_platform(tmp_path):
    target = tmp_path / "file.txt"
    result = long_path_safe(target)
    if os.name == "nt":
        assert str(result).startswith("\\\\?\\")
        assert str(target.resolve()) in str(result)
    else:
        assert result == target


def test_long_path_safe_does_not_double_prefix(tmp_path):
    target = tmp_path / "file.txt"
    once = long_path_safe(target)
    twice = long_path_safe(once)
    assert str(twice).count("\\\\?\\") <= 1
