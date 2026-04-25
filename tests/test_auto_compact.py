"""Тесты для auto_compact loop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.auto_compact import _tick


def _make_runtime(*, agents: dict, workers: dict | None = None) -> MagicMock:
    """Минимальный stub FleetRuntime — нам нужны только agents/workers."""
    runtime = MagicMock()
    runtime.agents = agents
    runtime.workers = workers or {}
    return runtime


def _make_agent(name: str, *, has_consolidator: bool, needs: bool):
    agent = MagicMock()
    agent.name = name
    if has_consolidator:
        consolidator = MagicMock()
        consolidator.needs_consolidation = MagicMock(return_value=needs)
        consolidator.consolidate = AsyncMock(return_value="summary")
        agent.consolidator = consolidator
    else:
        agent.consolidator = None
    return agent


def _make_worker(busy: bool):
    worker = MagicMock()
    worker.is_busy = MagicMock(return_value=busy)
    return worker


class TestAutoCompactTick:
    @pytest.mark.asyncio
    async def test_compacts_idle_agent_at_threshold(self):
        """Агент с consolidator в простое и нужно сжимать → consolidate вызван."""
        agent = _make_agent("alice", has_consolidator=True, needs=True)
        worker = _make_worker(busy=False)
        runtime = _make_runtime(agents={"alice": agent}, workers={"alice": worker})

        await _tick(runtime)

        agent.consolidator.consolidate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_busy_agent(self):
        """Worker занят → consolidate НЕ вызван (иначе можем затереть сессию)."""
        agent = _make_agent("alice", has_consolidator=True, needs=True)
        worker = _make_worker(busy=True)
        runtime = _make_runtime(agents={"alice": agent}, workers={"alice": worker})

        await _tick(runtime)

        agent.consolidator.consolidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_not_needed(self):
        """needs_consolidation=False → consolidate НЕ вызван."""
        agent = _make_agent("alice", has_consolidator=True, needs=False)
        runtime = _make_runtime(
            agents={"alice": agent},
            workers={"alice": _make_worker(busy=False)},
        )

        await _tick(runtime)

        agent.consolidator.consolidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_agent_without_consolidator(self):
        """Агент с consolidator=None → пропускаем без AttributeError."""
        agent = _make_agent("alice", has_consolidator=False, needs=False)
        runtime = _make_runtime(agents={"alice": agent})

        # Не должно бросить
        await _tick(runtime)

    @pytest.mark.asyncio
    async def test_one_agent_error_does_not_stop_others(self):
        """Если consolidate одного агента упал — другие всё равно жмутся."""
        bad = _make_agent("bad", has_consolidator=True, needs=True)
        bad.consolidator.consolidate = AsyncMock(side_effect=RuntimeError("boom"))

        good = _make_agent("good", has_consolidator=True, needs=True)

        runtime = _make_runtime(
            agents={"bad": bad, "good": good},
            workers={
                "bad": _make_worker(busy=False),
                "good": _make_worker(busy=False),
            },
        )

        await _tick(runtime)

        # good всё равно был сжат, несмотря на ошибку bad
        good.consolidator.consolidate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_worker_means_compact_anyway(self):
        """Если worker не зарегистрирован (старт ещё не завершён) — жмём."""
        agent = _make_agent("alice", has_consolidator=True, needs=True)
        runtime = _make_runtime(agents={"alice": agent}, workers={})

        await _tick(runtime)

        agent.consolidator.consolidate.assert_awaited_once()
