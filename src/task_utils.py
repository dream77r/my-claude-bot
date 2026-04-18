"""
Utilities for background asyncio tasks.

`spawn_supervised` replaces `asyncio.create_task(coro)` in fire-and-forget
paths where the caller has no intent to await the result. It keeps a
strong reference to the task (so the garbage collector can't cancel it
mid-flight) and logs any exception the task raises — otherwise it would
surface only as "Task exception was never retrieved" and be silently
lost.

Use it for background triggers, cron jobs, and similar one-shot work
that must not fail silently. Do not use it for long-running loops where
the caller already saves the task reference and awaits/cancels it
explicitly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Strong references for tasks whose caller didn't keep one. Tasks remove
# themselves when done via the callback below, so this set stays bounded.
_live_tasks: set[asyncio.Task] = set()


def _on_task_done(task: asyncio.Task) -> None:
    _live_tasks.discard(task)
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.error(
            f"Supervised task '{task.get_name()}' failed: {exc}",
            exc_info=exc,
        )


def spawn_supervised(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task:
    """Create a task with a strong reference and exception logging.

    Use for fire-and-forget background work whose failure would
    otherwise vanish into `Task exception was never retrieved`.
    """
    task = asyncio.create_task(coro, name=name)
    _live_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def live_task_count() -> int:
    """Return the number of currently-tracked supervised tasks."""
    return len(_live_tasks)
