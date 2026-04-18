"""Тесты для agent_worker.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent_worker import AgentWorker
from src.bus import FleetBus, FleetMessage, MessageType


def make_mock_agent(name: str = "test"):
    agent = MagicMock()
    agent.name = name
    agent.agent_dir = f"/tmp/agents/{name}"
    agent.call_claude = AsyncMock(return_value="Ответ от Claude")
    return agent


@pytest.fixture
def bus():
    return FleetBus()


@pytest.fixture
def worker(bus):
    agent = make_mock_agent()
    bus.subscribe("agent:test")
    sem = asyncio.Semaphore(1)
    return AgentWorker(agent, bus, sem)


class TestAgentWorker:
    @pytest.mark.asyncio
    async def test_handle_message(self, worker, bus):
        """Worker обрабатывает сообщение и публикует ответ."""
        # Подписаться на ответы
        bus.subscribe("telegram:test")

        msg = FleetMessage(
            source="telegram:123",
            target="agent:test",
            content="Привет",
            msg_type=MessageType.INBOUND,
            chat_id=123,
        )

        await worker._handle_message(msg)

        # Проверить что call_claude вызван
        worker.agent.call_claude.assert_called_once()
        call_args = worker.agent.call_claude.call_args
        assert call_args[0][0] == "Привет"

        # Проверить что ответ опубликован
        # Должно быть 2 сообщения: processing_started + response
        msgs = []
        while not bus._queues["telegram:test"].empty():
            msgs.append(bus._queues["telegram:test"].get_nowait())

        events = [m.metadata.get("event") for m in msgs]
        assert "processing_started" in events
        assert "response" in events

        response_msg = [m for m in msgs if m.metadata.get("event") == "response"][0]
        assert response_msg.content == "Ответ от Claude"
        assert response_msg.chat_id == 123

    @pytest.mark.asyncio
    async def test_handle_error(self, worker, bus):
        """Worker обрабатывает ошибку Claude."""
        bus.subscribe("telegram:test")
        worker.agent.call_claude = AsyncMock(side_effect=RuntimeError("boom"))

        msg = FleetMessage(
            source="telegram:123",
            target="agent:test",
            content="Привет",
            chat_id=123,
        )

        await worker._handle_message(msg)

        # Должен быть error-сообщение
        msgs = []
        while not bus._queues["telegram:test"].empty():
            msgs.append(bus._queues["telegram:test"].get_nowait())

        error_msgs = [m for m in msgs if m.metadata.get("event") == "error"]
        assert len(error_msgs) == 1
        assert "ошибка" in error_msgs[0].content.lower()

    def test_cancel_task(self, worker):
        """cancel_task отменяет активную задачу."""
        task = MagicMock()
        task.done.return_value = False
        worker._active_tasks[123] = task

        assert worker.cancel_task(123) is True
        task.cancel.assert_called_once()

    def test_cancel_task_no_active(self, worker):
        """cancel_task возвращает False если нет задачи."""
        assert worker.cancel_task(999) is False


class TestStreamInterruption:
    @pytest.mark.asyncio
    async def test_preempt_cancels_running_task(self, worker):
        """Активная задача отменяется при поступлении нового сообщения."""
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_call(*args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "not reached"

        worker.agent.call_claude = AsyncMock(side_effect=slow_call)

        msg = FleetMessage(
            source="telegram:123",
            target="agent:test",
            content="first",
            chat_id=123,
        )
        task = asyncio.create_task(worker._handle_message(msg))
        worker._active_tasks[123] = task
        await started.wait()
        assert 123 in worker._active_tasks

        preempted = await worker._preempt_active_task(123)
        assert preempted is True
        assert cancelled.is_set()
        # Identity-check в finally должен был удалить запись сам
        assert 123 not in worker._active_tasks

    @pytest.mark.asyncio
    async def test_preempt_noop_when_no_active_task(self, worker):
        assert await worker._preempt_active_task(999) is False

    @pytest.mark.asyncio
    async def test_preempt_noop_when_task_done(self, worker):
        done = asyncio.get_event_loop().create_future()
        done.set_result(None)
        worker._active_tasks[123] = done  # type: ignore[assignment]
        assert await worker._preempt_active_task(123) is False

    @pytest.mark.asyncio
    async def test_finally_does_not_evict_successor(self, worker, bus):
        """Identity-check: finally старой задачи не должен удалять новую запись.

        Это регрессионный тест на race condition: раньше `_active_tasks.pop`
        был безусловным и мог выкинуть task-преемника из словаря.
        """
        bus.subscribe("telegram:test")

        async def finishing_call(*args, **kwargs):
            return "fast"

        worker.agent.call_claude = AsyncMock(side_effect=finishing_call)

        # Запускаем первую задачу и даём ей завершиться
        first_msg = FleetMessage(
            source="telegram:123", target="agent:test",
            content="m1", chat_id=123,
        )
        first_task = asyncio.create_task(worker._handle_message(first_msg))
        worker._active_tasks[123] = first_task

        # Перед завершением первой — перезаписываем запись будущей-second task
        # (эмулируем ситуацию когда run() уже подменил запись)
        sentinel_task = asyncio.create_task(asyncio.sleep(0))
        worker._active_tasks[123] = sentinel_task

        await first_task  # ждём чтобы finally отработал

        # Запись sentinel должна сохраниться
        assert worker._active_tasks.get(123) is sentinel_task
        sentinel_task.cancel()
        try:
            await sentinel_task
        except asyncio.CancelledError:
            pass
