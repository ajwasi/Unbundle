"""Shared "fire and forget, but don't let it get garbage-collected" helper.

An asyncio.Task with its return value stored nowhere is eligible for
garbage collection as soon as nothing else references it — a documented
asyncio hazard (see asyncio.create_task's own docs), not a hypothetical
one: confirmed as the real cause of an intermittent test failure
(tests/test_downloads_worker.py) where a spawned task got collected
mid-flight under a busy full-suite run, before its own cleanup had a
chance to run. downloads/worker.py and audible/pdf_downloader.py each
independently reinvented the same module-level "hold a strong reference
in a set, drop it via a done-callback" fix — this is that fix, extracted
once.
"""

import asyncio
from typing import Coroutine

_background_tasks: set[asyncio.Task] = set()


def spawn_background_task(coro: Coroutine) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
