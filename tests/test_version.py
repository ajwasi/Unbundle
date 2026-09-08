import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app import version


def test_get_version_reads_version_file_when_present(tmp_path, monkeypatch):
    version_file = tmp_path / "VERSION"
    version_file.write_text("abc1234\n")
    monkeypatch.setattr(version, "_VERSION_FILE", version_file)

    assert version.get_version() == "abc1234"


def test_get_version_falls_back_to_git_when_no_version_file(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_VERSION_FILE", tmp_path / "does-not-exist")
    fake_result = MagicMock(returncode=0, stdout="deadbee\n")
    with patch("app.version.subprocess.run", return_value=fake_result) as mock_run:
        result = version.get_version()
    assert result == "deadbee"
    mock_run.assert_called_once()


def test_get_version_returns_unknown_when_neither_available(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_VERSION_FILE", tmp_path / "does-not-exist")
    fake_result = MagicMock(returncode=128, stdout="")
    with patch("app.version.subprocess.run", return_value=fake_result):
        assert version.get_version() == "unknown"


def test_get_version_returns_unknown_when_git_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_VERSION_FILE", tmp_path / "does-not-exist")
    with patch("app.version.subprocess.run", side_effect=OSError("git not found")):
        assert version.get_version() == "unknown"


@pytest.fixture(autouse=True)
def _reset_update_state():
    version._update_available = None
    yield
    version._update_available = None


def test_is_update_available_defaults_false_before_any_check():
    assert version.is_update_available() is False


async def test_check_for_update_sets_true_when_a_newer_commit_exists(monkeypatch):
    monkeypatch.setattr(version, "get_version", lambda: "abc1234")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"sha": "9999999999999999999999999999999999999999"}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
        await version.check_for_update()
    assert version.is_update_available() is True


async def test_check_for_update_sets_false_when_already_current(monkeypatch):
    monkeypatch.setattr(version, "get_version", lambda: "abc1234")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"sha": "abc1234extra_padding_to_be_a_real_sha_len"}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
        await version.check_for_update()
    assert version.is_update_available() is False


async def test_check_for_update_skips_when_version_is_unknown(monkeypatch):
    monkeypatch.setattr(version, "get_version", lambda: "unknown")
    with patch("httpx.AsyncClient.get", new=AsyncMock()) as mock_get:
        await version.check_for_update()
    mock_get.assert_not_awaited()
    assert version.is_update_available() is False


async def test_check_for_update_swallows_network_errors(monkeypatch):
    monkeypatch.setattr(version, "get_version", lambda: "abc1234")
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("no network"))):
        await version.check_for_update()  # must not raise
    assert version.is_update_available() is False
