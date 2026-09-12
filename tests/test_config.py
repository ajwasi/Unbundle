"""app/config.py's secret-key auto-generation — see _resolve_secret_key's own
docstring for the reasoning. Tested directly against a tmp_path rather than
through the real settings.data_dir, which in this test suite still resolves
to the class default (no DATA_DIR override in conftest.py) and would
otherwise write a stray .secret_key file into the real repo checkout.
"""

import os
from pathlib import Path

from app.config import DEFAULT_SECRET_KEY, SECRET_KEY_FILENAME, Settings, _resolve_secret_key


def test_resolve_secret_key_returns_explicit_value_unchanged(tmp_path):
    assert _resolve_secret_key("a-real-configured-key", tmp_path) == "a-real-configured-key"
    assert not (tmp_path / SECRET_KEY_FILENAME).exists()  # never touches disk when already configured


def test_resolve_secret_key_treats_blank_the_same_as_unconfigured(tmp_path):
    # A real .env file (or a Portainer stack) can easily carry an *empty*
    # APP_SECRET_KEY= through to here — must trigger generation, not pass ""
    # through as if it were a deliberately-chosen key.
    key = _resolve_secret_key("", tmp_path)
    assert key and key != DEFAULT_SECRET_KEY


def test_resolve_secret_key_generates_and_persists_when_nothing_configured(tmp_path):
    key = _resolve_secret_key(DEFAULT_SECRET_KEY, tmp_path)
    assert key != DEFAULT_SECRET_KEY
    saved = tmp_path / SECRET_KEY_FILENAME
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8").strip() == key


def test_resolve_secret_key_reuses_an_already_persisted_key_instead_of_regenerating(tmp_path):
    first = _resolve_secret_key(DEFAULT_SECRET_KEY, tmp_path)
    second = _resolve_secret_key(DEFAULT_SECRET_KEY, tmp_path)
    assert first == second


def test_resolve_secret_key_creates_data_dir_if_missing(tmp_path):
    fresh = tmp_path / "not-yet-created"
    key = _resolve_secret_key(DEFAULT_SECRET_KEY, fresh)
    assert (fresh / SECRET_KEY_FILENAME).read_text(encoding="utf-8").strip() == key


def test_resolve_secret_key_persisted_file_is_owner_only_on_posix(tmp_path):
    if os.name == "nt":
        return  # POSIX permission bits aren't meaningful on Windows
    _resolve_secret_key(DEFAULT_SECRET_KEY, tmp_path)
    mode = (tmp_path / SECRET_KEY_FILENAME).stat().st_mode
    assert mode & 0o777 == 0o600


def test_scan_root_list_defaults_to_the_single_scan_root_dir():
    settings = Settings(scan_root_dir=Path("/scan-root"), scan_roots="")
    assert settings.scan_root_list() == [Path("/scan-root")]


def test_scan_root_list_parses_comma_separated_roots():
    settings = Settings(scan_roots="/scan-root,/scan-root-comics")
    assert settings.scan_root_list() == [Path("/scan-root"), Path("/scan-root-comics")]


def test_scan_root_list_strips_whitespace_and_skips_empty_entries():
    settings = Settings(scan_roots=" /scan-root , , /scan-root-comics ")
    assert settings.scan_root_list() == [Path("/scan-root"), Path("/scan-root-comics")]
