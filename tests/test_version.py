from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app import version


def test_get_version_reads_version_file_when_present(tmp_path, monkeypatch):
    version_file = tmp_path / "VERSION"
    version_file.write_text("v1.2.3\n")
    monkeypatch.setattr(version, "_VERSION_FILE", version_file)

    assert version.get_version() == "v1.2.3"


def test_get_version_falls_back_to_git_when_no_version_file(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_VERSION_FILE", tmp_path / "does-not-exist")
    monkeypatch.setattr(version, "_git_describe_or_sha", lambda cwd: "deadbee")
    assert version.get_version() == "deadbee"


def test_get_version_returns_unknown_when_git_lookup_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_VERSION_FILE", tmp_path / "does-not-exist")
    monkeypatch.setattr(version, "_git_describe_or_sha", lambda cwd: None)
    assert version.get_version() == "unknown"


def test_git_describe_or_sha_prefers_an_exact_tag_match(tmp_path):
    fake_describe = MagicMock(returncode=0, stdout="v1.2.3\n")
    with patch("app.version.subprocess.run", return_value=fake_describe) as mock_run:
        result = version._git_describe_or_sha(tmp_path)
    assert result == "v1.2.3"
    mock_run.assert_called_once()  # never needed the rev-parse fallback


def test_git_describe_or_sha_falls_back_to_short_sha_when_head_is_not_a_tag(tmp_path):
    fake_describe = MagicMock(returncode=128, stdout="")  # git's real exit code for "no tag exactly matches"
    fake_rev_parse = MagicMock(returncode=0, stdout="deadbee\n")
    with patch("app.version.subprocess.run", side_effect=[fake_describe, fake_rev_parse]) as mock_run:
        result = version._git_describe_or_sha(tmp_path)
    assert result == "deadbee"
    assert mock_run.call_count == 2


def test_git_describe_or_sha_returns_none_when_neither_available(tmp_path):
    fake_result = MagicMock(returncode=128, stdout="")
    with patch("app.version.subprocess.run", return_value=fake_result):
        assert version._git_describe_or_sha(tmp_path) is None


def test_git_describe_or_sha_returns_none_when_git_not_installed(tmp_path):
    with patch("app.version.subprocess.run", side_effect=OSError("git not found")):
        assert version._git_describe_or_sha(tmp_path) is None


@pytest.fixture(autouse=True)
def _reset_update_state():
    version._update_available = None
    version._latest_tag = None
    yield
    version._update_available = None
    version._latest_tag = None


def test_is_update_available_defaults_false_before_any_check():
    assert version.is_update_available() is False


def test_latest_tag_defaults_none_before_any_check():
    assert version.latest_tag() is None


async def test_check_for_update_sets_true_and_records_latest_tag_when_a_newer_one_exists(monkeypatch):
    monkeypatch.setattr(version, "get_version", lambda: "v1.2.3")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = [{"name": "v1.2.3"}, {"name": "v1.3.0"}]
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
        await version.check_for_update()
    assert version.is_update_available() is True
    assert version.latest_tag() == "v1.3.0"


async def test_check_for_update_sets_false_when_already_on_the_latest_tag(monkeypatch):
    monkeypatch.setattr(version, "get_version", lambda: "v1.3.0")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = [{"name": "v1.2.3"}, {"name": "v1.3.0"}]
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
        await version.check_for_update()
    assert version.is_update_available() is False
    assert version.latest_tag() == "v1.3.0"


async def test_check_for_update_compares_by_numeric_semver_not_string_order(monkeypatch):
    # A plain string comparison would rank "v0.9.0" above "v0.10.0" (comparing
    # "9" against "1" character-by-character) — locks in the numeric fix.
    monkeypatch.setattr(version, "get_version", lambda: "v0.9.0")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = [{"name": "v0.9.0"}, {"name": "v0.10.0"}]
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
        await version.check_for_update()
    assert version.latest_tag() == "v0.10.0"
    assert version.is_update_available() is True


async def test_check_for_update_ignores_tags_that_are_not_plain_semver(monkeypatch):
    monkeypatch.setattr(version, "get_version", lambda: "v1.0.0")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = [{"name": "v1.0.0"}, {"name": "not-a-version"}, {"name": "v1.0.0-rc1"}]
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
        await version.check_for_update()
    assert version.latest_tag() == "v1.0.0"
    assert version.is_update_available() is False


async def test_check_for_update_skips_when_version_is_unknown(monkeypatch):
    monkeypatch.setattr(version, "get_version", lambda: "unknown")
    with patch("httpx.AsyncClient.get", new=AsyncMock()) as mock_get:
        await version.check_for_update()
    mock_get.assert_not_awaited()
    assert version.is_update_available() is False


async def test_check_for_update_skips_when_current_version_is_a_bare_sha_not_a_tag(monkeypatch):
    # A plain main build (or local dev) reports a bare commit SHA — nothing
    # meaningful to compare that against a semver tag, so this must not even
    # make a network call.
    monkeypatch.setattr(version, "get_version", lambda: "abc1234")
    with patch("httpx.AsyncClient.get", new=AsyncMock()) as mock_get:
        await version.check_for_update()
    mock_get.assert_not_awaited()
    assert version.is_update_available() is False


async def test_check_for_update_swallows_network_errors(monkeypatch):
    monkeypatch.setattr(version, "get_version", lambda: "v1.2.3")
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("no network"))):
        await version.check_for_update()  # must not raise
    assert version.is_update_available() is False
    assert version.latest_tag() is None
