from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.downloads.runner import run_download_job


def _fake_process(returncode=0, stdout=b"done"):
    proc = MagicMock()
    proc.stdout.read = AsyncMock(return_value=stdout)
    proc.wait = AsyncMock(return_value=None)
    proc.returncode = returncode
    return proc


@pytest.mark.asyncio
async def test_omits_i_flag_when_no_indices(tmp_path):
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_fake_process())) as mock_exec:
        await run_download_job("humble-cli", "GK1", None, None, tmp_path)
    argv = mock_exec.call_args[0]
    assert argv == ("humble-cli", "download", "GK1")


@pytest.mark.asyncio
async def test_includes_sorted_deduped_indices(tmp_path):
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_fake_process())) as mock_exec:
        await run_download_job("humble-cli", "GK1", [3, 1, 1, 2], None, tmp_path)
    argv = mock_exec.call_args[0]
    assert argv == ("humble-cli", "download", "GK1", "-i", "1,2,3")


@pytest.mark.asyncio
async def test_includes_repeated_sorted_deduped_format_flags(tmp_path):
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_fake_process())) as mock_exec:
        await run_download_job("humble-cli", "GK1", None, ["PDF", "EPUB", "EPUB"], tmp_path)
    argv = mock_exec.call_args[0]
    assert argv == ("humble-cli", "download", "GK1", "-f", "EPUB", "-f", "PDF")


@pytest.mark.asyncio
async def test_combines_indices_and_formats(tmp_path):
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_fake_process())) as mock_exec:
        await run_download_job("humble-cli", "GK1", [2], ["MOBI"], tmp_path)
    argv = mock_exec.call_args[0]
    assert argv == ("humble-cli", "download", "GK1", "-i", "2", "-f", "MOBI")


@pytest.mark.asyncio
async def test_creates_cwd_directory(tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_fake_process())):
        await run_download_job("humble-cli", "GK1", None, None, target)
    assert target.is_dir()


@pytest.mark.asyncio
async def test_returns_returncode_and_decoded_output(tmp_path):
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_fake_process(returncode=1, stdout=b"boom"))):
        returncode, output = await run_download_job("humble-cli", "GK1", None, None, tmp_path)
    assert returncode == 1
    assert output == "boom"


@pytest.mark.asyncio
async def test_runs_with_cwd_set_to_downloads_dir(tmp_path):
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_fake_process())) as mock_exec:
        await run_download_job("humble-cli", "GK1", None, None, tmp_path)
    assert mock_exec.call_args.kwargs["cwd"] == str(tmp_path)
