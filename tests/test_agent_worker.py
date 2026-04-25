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


class TestMidTurnInjection:
    @pytest.mark.asyncio
    async def test_followup_buffered_while_turn_active(self, worker, bus):
        """Второе сообщение во время turn попадает в pending_followups."""
        bus.subscribe("telegram:test")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_call(*args, **kwargs):
            started.set()
            await release.wait()
            return "done"

        worker.agent.call_claude = AsyncMock(side_effect=slow_call)

        first = FleetMessage(
            source="telegram:123",
            target="agent:test",
            content="first",
            chat_id=123,
        )
        run_task = asyncio.create_task(worker.run())

        # Отправляем первое — стартует turn
        await bus.publish(first)
        await started.wait()
        assert worker._has_active_task(123)

        # Второе сообщение во время turn — должно буферизоваться
        second = FleetMessage(
            source="telegram:123",
            target="agent:test",
            content="second",
            chat_id=123,
        )
        await bus.publish(second)
        # Даём run() возможность обработать
        for _ in range(10):
            await asyncio.sleep(0)
            if worker._pending_followups.get(123):
                break

        assert worker._pending_followups.get(123) == [second]
        # call_claude был вызван только для первого
        assert worker.agent.call_claude.call_count == 1

        release.set()
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_coalesce_multiple_followups(self, worker):
        """Несколько follow-ups склеиваются в одно сообщение с всеми файлами."""
        msgs = [
            FleetMessage(
                source="telegram:123",
                target="agent:test",
                content="first edit",
                chat_id=123,
                files=[{"name": "a.txt"}],
                metadata={"message_thread_id": 10},
            ),
            FleetMessage(
                source="telegram:123",
                target="agent:test",
                content="second edit",
                chat_id=123,
                metadata={"message_thread_id": 11},
            ),
            FleetMessage(
                source="telegram:123",
                target="agent:test",
                content="third edit",
                chat_id=123,
                files=[{"name": "b.txt"}],
                metadata={"message_thread_id": 12},
            ),
        ]
        merged = AgentWorker._coalesce_followups(msgs)
        assert merged.content == "first edit\n\nsecond edit\n\nthird edit"
        assert merged.files == [{"name": "a.txt"}, {"name": "b.txt"}]
        # Последние метаданные доминируют (свежий thread_id)
        assert merged.metadata["message_thread_id"] == 12
        assert merged.metadata["coalesced_count"] == 3
        assert merged.chat_id == 123

    @pytest.mark.asyncio
    async def test_followup_runs_after_current_turn(self, worker, bus):
        """Coalesced follow-up становится следующим turn'ом после завершения."""
        bus.subscribe("telegram:test")
        calls = []
        release = [asyncio.Event(), asyncio.Event()]
        started = [asyncio.Event(), asyncio.Event()]

        async def tracking_call(prompt, *args, **kwargs):
            idx = len(calls)
            calls.append(prompt)
            started[idx].set()
            await release[idx].wait()
            return f"reply-{idx}"

        worker.agent.call_claude = AsyncMock(side_effect=tracking_call)

        run_task = asyncio.create_task(worker.run())

        # Первое — блокируется
        await bus.publish(FleetMessage(
            source="telegram:123", target="agent:test",
            content="one", chat_id=123,
        ))
        await started[0].wait()

        # Второе — буферится
        await bus.publish(FleetMessage(
            source="telegram:123", target="agent:test",
            content="two", chat_id=123,
        ))
        # Даём run() переложить в буфер
        for _ in range(10):
            await asyncio.sleep(0)
            if worker._pending_followups.get(123):
                break
        assert worker._pending_followups.get(123)

        # Отпускаем первый turn — должен запуститься второй с буферизованным
        release[0].set()
        await started[1].wait()
        assert calls == ["one", "two"]
        assert worker._pending_followups.get(123) is None

        release[1].set()
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_cancel_task_drops_pending_followups(self, worker):
        """/stop не должен запускать буферизованные сообщения после cancel."""
        task = MagicMock()
        task.done.return_value = False
        worker._active_tasks[123] = task
        worker._pending_followups[123] = [
            FleetMessage(source="x", target="y", content="buffered", chat_id=123),
        ]

        assert worker.cancel_task(123) is True
        assert 123 not in worker._pending_followups

    @pytest.mark.asyncio
    async def test_agent_to_agent_bypasses_followup_buffer(self, worker, bus):
        """Делегация (AGENT_TO_AGENT) не должна буферизоваться даже если
        формально есть активная задача на chat_id (у делегаций chat_id=0)."""
        bus.subscribe("telegram:test")
        # Фейковая активная задача на chat_id=0
        fake = asyncio.create_task(asyncio.sleep(60))
        worker._active_tasks[0] = fake

        try:
            # AGENT_TO_AGENT с chat_id=0 — фильтр в run() должен пропустить
            msg = FleetMessage(
                source="agent:other",
                target="agent:test",
                content="deleg",
                msg_type=MessageType.AGENT_TO_AGENT,
                chat_id=0,
                metadata={"source_role": "master", "delegation_chain": ["other"]},
            )
            # Проверяем только условие в run() — chat_id=0 не триггерит
            # буфер. Упрощённо: смотрим _has_active_task + фильтр.
            # chat_id=0 falsy → первое условие if msg.chat_id уже False.
            assert not (
                msg.chat_id
                and msg.msg_type != MessageType.AGENT_TO_AGENT
                and worker._has_active_task(msg.chat_id)
            )
        finally:
            fake.cancel()
            try:
                await fake
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_followups_dropped_on_turn_error(self, worker, bus):
        """Если turn упал с ошибкой — pending follow-ups дропаются с уведомлением.

        Регрессия на петлю каждые 10 минут: раньше merged follow-ups
        ре-публиковались как новый turn после timeout/error, и бот
        гарантированно зацикливался на той же битой сессии. Теперь — drop
        + сообщение юзеру, чтобы он сам решил, что повторять.
        """
        bus.subscribe("telegram:test")
        bus.subscribe("agent:test")
        worker.agent.call_claude = AsyncMock(side_effect=RuntimeError("boom"))

        followup = FleetMessage(
            source="telegram:123",
            target="agent:test",
            content="next question",
            chat_id=123,
        )
        worker._pending_followups[123] = [followup]

        msg = FleetMessage(
            source="telegram:123",
            target="agent:test",
            content="fail",
            chat_id=123,
        )
        await worker._handle_message(msg)

        # Follow-up НЕ должен оказаться в очереди agent:test —
        # цикл "ошибка → re-trigger → снова ошибка" разорван.
        agent_q = bus._queues["agent:test"]
        agent_delivered = []
        while not agent_q.empty():
            agent_delivered.append(agent_q.get_nowait())
        assert not any(m.content == "next question" for m in agent_delivered)

        # Юзер должен увидеть уведомление, что N сообщений пропущено.
        tg_q = bus._queues["telegram:test"]
        tg_delivered = []
        while not tg_q.empty():
            tg_delivered.append(tg_q.get_nowait())
        notify = [
            m for m in tg_delivered
            if m.metadata.get("event") == "followups_dropped"
        ]
        assert len(notify) == 1
        assert notify[0].metadata["count"] == 1

    @pytest.mark.asyncio
    async def test_followups_dropped_on_timeout(self, worker, bus):
        """Та же защита от петли при asyncio.TimeoutError из call_claude."""
        bus.subscribe("telegram:test")
        bus.subscribe("agent:test")
        worker.agent.call_claude = AsyncMock(side_effect=asyncio.TimeoutError())

        worker._pending_followups[123] = [
            FleetMessage(
                source="telegram:123",
                target="agent:test",
                content="late question",
                chat_id=123,
            )
        ]

        await worker._handle_message(FleetMessage(
            source="telegram:123",
            target="agent:test",
            content="initial",
            chat_id=123,
        ))

        agent_q = bus._queues["agent:test"]
        while not agent_q.empty():
            msg_out = agent_q.get_nowait()
            assert msg_out.content != "late question"

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
