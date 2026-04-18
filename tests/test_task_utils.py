"""Tests for src/task_utils.spawn_supervised."""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.task_utils import live_task_count, spawn_supervised


@pytest.mark.asyncio
async def test_exception_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def boom() -> None:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR, logger="src.task_utils"):
        task = spawn_supervised(boom(), name="unit-boom")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done()

    assert any(
        "unit-boom" in rec.message and "kaboom" in rec.message
        for rec in caplog.records
    ), caplog.records


@pytest.mark.asyncio
async def test_cancellation_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def forever() -> None:
        await asyncio.sleep(30)

    with caplog.at_level(logging.ERROR, logger="src.task_utils"):
        task = spawn_supervised(forever(), name="unit-cancel")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert not any(
        "unit-cancel" in rec.message for rec in caplog.records
    ), "cancellation should not log an error"


@pytest.mark.asyncio
async def test_strong_reference_prevents_gc() -> None:
    """Task должен дожить до завершения, даже если caller не держит ссылку."""
    completed = asyncio.Event()

    async def worker() -> None:
        await asyncio.sleep(0.05)
        completed.set()

    # Не сохраняем возвращаемый task вообще.
    spawn_supervised(worker(), name="unit-gc")
    import gc

    gc.collect()
    await asyncio.wait_for(completed.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_live_task_count_cleans_up() -> None:
    before = live_task_count()

    async def quick() -> None:
        await asyncio.sleep(0)

    tasks = [spawn_supervised(quick(), name=f"unit-{i}") for i in range(3)]
    await asyncio.gather(*tasks)
    # Дать callback'у отработать
    await asyncio.sleep(0)
    assert live_task_count() == before


@pytest.mark.asyncio
async def test_successful_task_does_not_log_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def ok() -> str:
        return "fine"

    with caplog.at_level(logging.ERROR, logger="src.task_utils"):
        task = spawn_supervised(ok(), name="unit-ok")
        await task

    assert not any("failed" in rec.message for rec in caplog.records)
