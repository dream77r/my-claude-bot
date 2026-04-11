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
