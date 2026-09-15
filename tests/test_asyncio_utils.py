import asyncio

import pytest

from app.asyncio_utils import _background_tasks, spawn_background_task


@pytest.mark.asyncio
async def test_spawn_background_task_runs_the_coroutine():
    result = {}

    async def _set():
        result["ran"] = True

    task = spawn_background_task(_set())
    await task
    assert result["ran"] is True


@pytest.mark.asyncio
async def test_spawn_background_task_holds_a_reference_until_done():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _wait():
        started.set()
        await finish.wait()

    task = spawn_background_task(_wait())
    await started.wait()
    assert task in _background_tasks

    finish.set()
    await task
    await asyncio.sleep(0)  # done-callbacks run via call_soon, not synchronously with await
    assert task not in _background_tasks
