"""Тесты для cron.py."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.cron import CronJob, _execute_job, load_cron_jobs, parse_cron_field, should_run
from src.bus import FleetBus, FleetMessage, MessageType


class TestParseCronField:
    def test_wildcard(self):
        assert parse_cron_field("*", 5, 59) is True
        assert parse_cron_field("*", 0, 59) is True

    def test_exact(self):
        assert parse_cron_field("5", 5, 59) is True
        assert parse_cron_field("5", 6, 59) is False

    def test_step(self):
        assert parse_cron_field("*/15", 0, 59) is True
        assert parse_cron_field("*/15", 15, 59) is True
        assert parse_cron_field("*/15", 30, 59) is True
        assert parse_cron_field("*/15", 7, 59) is False

    def test_range(self):
        assert parse_cron_field("9-17", 9, 23) is True
        assert parse_cron_field("9-17", 12, 23) is True
        assert parse_cron_field("9-17", 17, 23) is True
        assert parse_cron_field("9-17", 8, 23) is False
        assert parse_cron_field("9-17", 18, 23) is False

    def test_list(self):
        assert parse_cron_field("1,3,5", 3, 6) is True
        assert parse_cron_field("1,3,5", 2, 6) is False


class TestShouldRun:
    def test_every_minute(self):
        now = datetime(2026, 4, 10, 14, 30)
        assert should_run("* * * * *", now) is True

    def test_specific_time(self):
        now = datetime(2026, 4, 10, 9, 0)
        assert should_run("0 9 * * *", now) is True
        assert should_run("0 10 * * *", now) is False

    def test_monday_9am(self):
        # 2026-04-13 is Monday
        monday = datetime(2026, 4, 13, 9, 0)
        assert should_run("0 9 * * 1", monday) is True

        tuesday = datetime(2026, 4, 14, 9, 0)
        assert should_run("0 9 * * 1", tuesday) is False

    def test_every_15_minutes(self):
        assert should_run("*/15 * * * *", datetime(2026, 1, 1, 12, 0)) is True
        assert should_run("*/15 * * * *", datetime(2026, 1, 1, 12, 15)) is True
        assert should_run("*/15 * * * *", datetime(2026, 1, 1, 12, 7)) is False

    def test_invalid_schedule(self):
        assert should_run("invalid", datetime(2026, 1, 1)) is False
        assert should_run("* * *", datetime(2026, 1, 1)) is False


class TestLoadCronJobs:
    def test_load_valid(self):
        config = {
            "cron": [
                {
                    "name": "test_job",
                    "schedule": "0 9 * * *",
                    "prompt": "Do something",
                    "model": "haiku",
                    "notify": True,
                }
            ]
        }
        jobs = load_cron_jobs(config)
        assert len(jobs) == 1
        assert jobs[0].name == "test_job"
        assert jobs[0].model == "haiku"

    def test_load_defaults(self):
        config = {
            "cron": [
                {
                    "name": "minimal",
                    "schedule": "* * * * *",
                    "prompt": "test",
                }
            ]
        }
        jobs = load_cron_jobs(config)
        assert jobs[0].model == "sonnet"
        assert jobs[0].notify is True

    def test_skip_invalid(self):
        config = {
            "cron": [
                {"name": "missing_fields"},  # нет schedule и prompt
                {
                    "name": "valid",
                    "schedule": "* * * * *",
                    "prompt": "ok",
                },
            ]
        }
        jobs = load_cron_jobs(config)
        assert len(jobs) == 1
        assert jobs[0].name == "valid"

    def test_no_cron_section(self):
        jobs = load_cron_jobs({})
        assert jobs == []

    def test_multiple_jobs(self):
        config = {
            "cron": [
                {"name": "a", "schedule": "0 9 * * 1", "prompt": "weekly"},
                {"name": "b", "schedule": "0 21 * * *", "prompt": "daily"},
            ]
        }
        jobs = load_cron_jobs(config)
        assert len(jobs) == 2


class TestExecuteJobNotificationRouting:
    """Тесты маршрутизации уведомлений из _execute_job.

    Проверяем что:
    - мастер-агент шлёт в telegram:{agent_name} (нет notify_agent_name)
    - worker-агент шлёт в telegram:{notify_agent_name} (мастер)
    - chat_id=0 → сообщение уходит, но bus-listener его дропнет
    - notify=False → сообщений нет
    """

    def _make_job(self, notify: bool = True) -> CronJob:
        return CronJob(
            name="test_job",
            schedule="* * * * *",
            prompt="Say hello",
            model="haiku",
            notify=notify,
        )

    def _make_sdk_modules(self, text: str):
        """Создаём mock-объекты claude_agent_sdk + src.memory.

        _execute_job использует lazy imports:
          from claude_agent_sdk import AssistantMessage, ... query
          from . import memory
        Поэтому патчим через sys.modules.
        """
        import claude_agent_sdk as real_sdk  # убеждаемся, что SDK загружен

        # Используем НАСТОЯЩИЕ классы SDK — иначе isinstance-проверки в
        # _execute_job провалятся и result_text останется пустым.
        real_msg = real_sdk.AssistantMessage(
            content=[real_sdk.TextBlock(text=text)],
            model="haiku",
            session_id="test-session",
        )

        async def _fake_query(**kwargs):
            yield real_msg

        mock_sdk = MagicMock()
        mock_sdk.AssistantMessage = real_sdk.AssistantMessage
        mock_sdk.ResultMessage = real_sdk.ResultMessage
        mock_sdk.TextBlock = real_sdk.TextBlock
        mock_sdk.ClaudeAgentOptions = real_sdk.ClaudeAgentOptions
        mock_sdk.query = lambda **kw: _fake_query(**kw)

        # --- mock src.memory ---
        mock_memory = MagicMock()
        mock_memory.get_memory_path.return_value = "/tmp/test"
        mock_memory.git_commit = MagicMock()

        return mock_sdk, mock_memory

    @pytest.mark.asyncio
    async def test_master_agent_routes_to_own_channel(self):
        """Мастер-агент: target=telegram:{agent_name}."""
        import sys

        bus = FleetBus()
        bus.subscribe("telegram:me")

        job = self._make_job()
        mock_sdk, mock_memory = self._make_sdk_modules("Digest result")
        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "src.memory": mock_memory}):
            await _execute_job(
                job=job,
                agent_dir="/tmp/test",
                agent_name="me",
                bus=bus,
                chat_id=12345,
                notify_agent_name=None,  # мастер: без переопределения
            )

        # После выполнения сообщение должно быть в очереди
        assert not bus._queues["telegram:me"].empty()
        msg = bus._queues["telegram:me"].get_nowait()
        assert msg.target == "telegram:me"
        assert msg.chat_id == 12345
        assert "Digest result" in msg.content

    @pytest.mark.asyncio
    async def test_worker_agent_routes_to_master_channel(self):
        """Worker-агент: target=telegram:me (notify_agent_name='me')."""
        import sys

        bus = FleetBus()
        bus.subscribe("telegram:me")

        job = self._make_job()
        mock_sdk, mock_memory = self._make_sdk_modules("Worker cron result")
        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "src.memory": mock_memory}):
            await _execute_job(
                job=job,
                agent_dir="/tmp/test",
                agent_name="analyst",  # worker-агент
                bus=bus,
                chat_id=99999,          # FOUNDER_TELEGRAM_ID
                notify_agent_name="me", # маршрутизируем через мастера
            )

        assert not bus._queues["telegram:me"].empty()
        msg = bus._queues["telegram:me"].get_nowait()
        assert msg.target == "telegram:me"
        assert msg.chat_id == 99999
        assert msg.source == "agent:analyst"

    @pytest.mark.asyncio
    async def test_zero_chat_id_message_goes_to_own_channel(self):
        """chat_id=0 без notify_agent_name → идёт в telegram:analyst.

        Bus-listener дропнет его (chat_id=0), но в telegram:me ничего нет.
        """
        import sys

        bus = FleetBus()
        bus.subscribe("telegram:me")
        bus.subscribe("telegram:analyst")

        job = self._make_job()
        mock_sdk, mock_memory = self._make_sdk_modules("some result")
        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "src.memory": mock_memory}):
            await _execute_job(
                job=job,
                agent_dir="/tmp/test",
                agent_name="analyst",
                bus=bus,
                chat_id=0,
                notify_agent_name=None,  # не перенаправляем — уйдёт в telegram:analyst
            )

        # В telegram:me — пусто, в telegram:analyst — пришло с chat_id=0
        assert bus._queues["telegram:me"].empty()
        assert not bus._queues["telegram:analyst"].empty()
        msg = bus._queues["telegram:analyst"].get_nowait()
        assert msg.chat_id == 0

    @pytest.mark.asyncio
    async def test_notify_false_no_message_sent(self):
        """notify=False → никаких публикаций."""
        import sys

        bus = FleetBus()
        bus.subscribe("telegram:me")

        job = self._make_job(notify=False)
        mock_sdk, mock_memory = self._make_sdk_modules("silent result")
        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "src.memory": mock_memory}):
            await _execute_job(
                job=job,
                agent_dir="/tmp/test",
                agent_name="analyst",
                bus=bus,
                chat_id=99999,
                notify_agent_name="me",
            )

        assert bus._queues["telegram:me"].empty()
