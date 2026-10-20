from unittest.mock import patch

from app.fs_utils import safe_unlink


def test_safe_unlink_removes_an_existing_file(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    safe_unlink(f)
    assert not f.exists()


def test_safe_unlink_tolerates_a_missing_file(tmp_path):
    safe_unlink(tmp_path / "does-not-exist.txt")  # must not raise


def test_safe_unlink_tolerates_any_other_oserror(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with patch("pathlib.Path.unlink", side_effect=OSError(30, "Read-only file system")):
        safe_unlink(f)  # must not raise
