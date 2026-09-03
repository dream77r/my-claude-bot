"""Тесты для cron.py."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.cron import (
    CronJob,
    _execute_job,
    load_cron_jobs,
    parse_cron_field,
    parse_dom_field,
    resolve_last_day_token,
    should_run,
)
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


class TestExecuteJobZeroChatIdWarning:
    """_execute_job логирует warning когда chat_id=0 и notify=True."""

    def _make_sdk_modules(self, text: str):
        import claude_agent_sdk as real_sdk

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

        mock_memory = MagicMock()
        mock_memory.get_memory_path.return_value = "/tmp/test"
        mock_memory.git_commit = MagicMock()
        return mock_sdk, mock_memory

    @pytest.mark.asyncio
    async def test_warning_logged_when_chat_id_zero(self, caplog):
        """chat_id=0 + notify=True → warning в лог."""
        import logging
        import sys

        bus = FleetBus()
        bus.subscribe("telegram:worker")

        job = CronJob(
            name="daily_digest",
            schedule="* * * * *",
            prompt="test prompt",
            model="haiku",
            notify=True,
        )
        mock_sdk, mock_memory = self._make_sdk_modules("some result")
        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "src.memory": mock_memory}):
            with caplog.at_level(logging.WARNING, logger="src.cron"):
                await _execute_job(
                    job=job,
                    agent_dir="/tmp/test",
                    agent_name="worker",
                    bus=bus,
                    chat_id=0,
                    notify_agent_name=None,
                )

        assert any(
            "chat_id=0" in r.message and "FOUNDER_TELEGRAM_ID" in r.message
            for r in caplog.records
        ), "Ожидался warning про chat_id=0 в логе"

    @pytest.mark.asyncio
    async def test_no_warning_when_chat_id_nonzero(self, caplog):
        """chat_id != 0 → warning НЕ должен логироваться."""
        import logging
        import sys

        bus = FleetBus()
        bus.subscribe("telegram:me")

        job = CronJob(
            name="daily_digest",
            schedule="* * * * *",
            prompt="test prompt",
            model="haiku",
            notify=True,
        )
        mock_sdk, mock_memory = self._make_sdk_modules("some result")
        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "src.memory": mock_memory}):
            with caplog.at_level(logging.WARNING, logger="src.cron"):
                await _execute_job(
                    job=job,
                    agent_dir="/tmp/test",
                    agent_name="me",
                    bus=bus,
                    chat_id=99999,
                    notify_agent_name=None,
                )

        zero_warnings = [
            r for r in caplog.records if "chat_id=0" in r.message
        ]
        assert not zero_warnings


class TestFounderChatIdFallback:
    """Тесты fallback-цепочки _founder_chat_id."""

    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        """Env var задан → возвращаем его значение."""
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "111222333")
        from src.main import _founder_chat_id
        assert _founder_chat_id(tmp_path) == 111222333

    def test_fallback_from_settings_json(self, tmp_path, monkeypatch):
        """Env var не задан, telegram_id в settings.json → fallback работает."""
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)

        settings_dir = tmp_path / "agents" / "me" / "memory"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text(
            '{"telegram_id": 987654321, "language": "ru"}',
            encoding="utf-8",
        )

        from src.main import _founder_chat_id
        assert _founder_chat_id(tmp_path) == 987654321

    def test_fallback_from_settings_json_chat_id_key(self, tmp_path, monkeypatch):
        """Env var не задан, ключ chat_id в settings.json → fallback работает."""
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)

        settings_dir = tmp_path / "agents" / "me" / "memory"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text(
            '{"chat_id": 555000111}',
            encoding="utf-8",
        )

        from src.main import _founder_chat_id
        assert _founder_chat_id(tmp_path) == 555000111

    def test_fallback_from_profile_md(self, tmp_path, monkeypatch):
        """Env var не задан, telegram_id в profile.md → fallback работает."""
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)

        profile_dir = tmp_path / "agents" / "me" / "memory"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.md").write_text(
            "# Профиль\n\n- **telegram_id:** 777888999\n",
            encoding="utf-8",
        )

        from src.main import _founder_chat_id
        assert _founder_chat_id(tmp_path) == 777888999

    def test_settings_json_takes_precedence_over_profile_md(self, tmp_path, monkeypatch):
        """Оба файла существуют → settings.json приоритетнее profile.md."""
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)

        mem_dir = tmp_path / "agents" / "me" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "settings.json").write_text(
            '{"telegram_id": 100200300}', encoding="utf-8"
        )
        (mem_dir / "profile.md").write_text(
            "- telegram_id: 999888777\n", encoding="utf-8"
        )

        from src.main import _founder_chat_id
        assert _founder_chat_id(tmp_path) == 100200300

    def test_no_fallback_returns_zero_with_warning(self, tmp_path, monkeypatch, caplog):
        """Ни env, ни файлы → 0 + warning в лог."""
        import logging

        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        assert not (tmp_path / "agents").exists()

        from src.main import _founder_chat_id
        with caplog.at_level(logging.WARNING):
            result = _founder_chat_id(tmp_path)

        assert result == 0
        assert any(
            "FOUNDER_TELEGRAM_ID" in r.message for r in caplog.records
        ), "Ожидался warning про FOUNDER_TELEGRAM_ID"

    def test_env_var_invalid_falls_through_to_file(self, tmp_path, monkeypatch):
        """Env var содержит нечисловое значение → falls through к settings.json."""
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "not-a-number")

        mem_dir = tmp_path / "agents" / "me" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "settings.json").write_text(
            '{"telegram_id": 123456789}', encoding="utf-8"
        )

        from src.main import _founder_chat_id
        assert _founder_chat_id(tmp_path) == 123456789


class TestLastDayOfMonth:
    """`L` / `L-N` в поле дня месяца — для правил инвентаризации."""

    def test_resolve_token(self):
        # Февраль 2026 — 28 дней, февраль 2028 — високосный, 29
        assert resolve_last_day_token("L", datetime(2026, 2, 10)) == 28
        assert resolve_last_day_token("L-1", datetime(2026, 2, 10)) == 27
        assert resolve_last_day_token("L", datetime(2028, 2, 1)) == 29
        assert resolve_last_day_token("L", datetime(2026, 1, 1)) == 31

    def test_resolve_token_non_l(self):
        assert resolve_last_day_token("15", datetime(2026, 1, 1)) is None
        assert resolve_last_day_token("*", datetime(2026, 1, 1)) is None
        assert resolve_last_day_token("*/2", datetime(2026, 1, 1)) is None

    def test_last_day_31_day_month(self):
        assert should_run("0 10 L * *", datetime(2026, 1, 31, 10, 0)) is True
        assert should_run("0 10 L * *", datetime(2026, 1, 30, 10, 0)) is False

    def test_last_day_30_day_month(self):
        assert should_run("0 10 L * *", datetime(2026, 4, 30, 10, 0)) is True
        assert should_run("0 10 L * *", datetime(2026, 4, 29, 10, 0)) is False

    def test_last_day_february_common_and_leap(self):
        # 2026 — не високосный: последний день 28 февраля
        assert should_run("0 10 L * *", datetime(2026, 2, 28, 10, 0)) is True
        # 2028 — високосный: 28 февраля уже НЕ последний
        assert should_run("0 10 L * *", datetime(2028, 2, 28, 10, 0)) is False
        assert should_run("0 10 L * *", datetime(2028, 2, 29, 10, 0)) is True

    def test_day_before_last_day(self):
        assert should_run("0 16 L-1 * *", datetime(2026, 2, 27, 16, 0)) is True
        assert should_run("0 16 L-1 * *", datetime(2026, 2, 28, 16, 0)) is False
        assert should_run("0 16 L-1 * *", datetime(2026, 1, 30, 16, 0)) is True

    def test_hour_still_filters(self):
        assert should_run("0 10 L * *", datetime(2026, 1, 31, 16, 0)) is False

    def test_lowercase_l(self):
        assert should_run("0 10 l * *", datetime(2026, 1, 31, 10, 0)) is True

    def test_list_of_l_tokens(self):
        # "накануне последнего И последний" одним выражением
        assert parse_dom_field("L,L-1", datetime(2026, 1, 31)) is True
        assert parse_dom_field("L,L-1", datetime(2026, 1, 30)) is True
        assert parse_dom_field("L,L-1", datetime(2026, 1, 29)) is False

    def test_mixed_list_with_plain_day(self):
        assert parse_dom_field("1,L", datetime(2026, 1, 1)) is True
        assert parse_dom_field("1,L", datetime(2026, 1, 31)) is True
        assert parse_dom_field("1,L", datetime(2026, 1, 15)) is False

    def test_offset_beyond_month_never_fires(self):
        # L-40 уходит за начало месяца — не наступает никогда
        for day in (1, 15, 28):
            assert parse_dom_field("L-40", datetime(2026, 2, day)) is False

    def test_plain_fields_unchanged(self):
        # Регресс: поля без L работают ровно как раньше
        assert parse_dom_field("15", datetime(2026, 1, 15)) is True
        assert parse_dom_field("15", datetime(2026, 1, 16)) is False
        assert parse_dom_field("*", datetime(2026, 1, 16)) is True
        assert parse_dom_field("*/10", datetime(2026, 1, 20)) is True

    def test_garbage_field_does_not_raise(self):
        # Раньше ValueError из int() улетал в вызывающий cron-цикл
        assert should_run("0 9 abc * *", datetime(2026, 1, 15, 9, 0)) is False
        assert should_run("zz 9 * * *", datetime(2026, 1, 15, 9, 0)) is False


class TestCronJobTargetChat:
    """Явный chat_id/thread_id у задачи — отчёт в группу, а не в личку."""

    def _config(self, **overrides):
        item = {
            "name": "inventory",
            "schedule": "0 10 L * *",
            "prompt": "Напомни про инвентаризацию",
        }
        item.update(overrides)
        return {"cron": [item]}

    def test_defaults_to_none(self):
        job = load_cron_jobs(self._config())[0]
        assert job.chat_id is None
        assert job.thread_id is None

    def test_parsed_from_config(self):
        job = load_cron_jobs(
            self._config(chat_id=-1003804830025, thread_id=42)
        )[0]
        assert job.chat_id == -1003804830025
        assert job.thread_id == 42

    def test_string_chat_id_coerced(self):
        # YAML легко отдаёт строку, если значение в кавычках
        job = load_cron_jobs(self._config(chat_id="-1003804830025"))[0]
        assert job.chat_id == -1003804830025

    def test_garbage_chat_id_ignored(self):
        job = load_cron_jobs(self._config(chat_id="не число"))[0]
        assert job.chat_id is None

    @pytest.mark.asyncio
    async def test_job_chat_id_overrides_agent_chat(self):
        """chat_id задачи перекрывает дефолтный чат агента."""
        import sys

        helper = TestExecuteJobNotificationRouting()
        bus = FleetBus()
        bus.subscribe("telegram:me")

        job = CronJob(
            name="inventory",
            schedule="0 10 L * *",
            prompt="Напомни",
            model="haiku",
            notify=True,
            chat_id=-1003804830025,
            thread_id=7,
        )
        mock_sdk, mock_memory = helper._make_sdk_modules("Инвентаризация!")
        with patch.dict(
            sys.modules,
            {"claude_agent_sdk": mock_sdk, "src.memory": mock_memory},
        ):
            await _execute_job(
                job=job,
                agent_dir="/tmp/test",
                agent_name="me",
                bus=bus,
                chat_id=12345,  # личка владельца — должна быть перекрыта
            )

        msg = bus._queues["telegram:me"].get_nowait()
        assert msg.chat_id == -1003804830025
        assert msg.metadata["message_thread_id"] == 7
        assert "Инвентаризация!" in msg.content

    @pytest.mark.asyncio
    async def test_without_job_chat_id_falls_back_to_agent_chat(self):
        """Регресс: без chat_id у задачи поведение прежнее."""
        import sys

        helper = TestExecuteJobNotificationRouting()
        bus = FleetBus()
        bus.subscribe("telegram:me")

        job = helper._make_job()
        mock_sdk, mock_memory = helper._make_sdk_modules("Дайджест")
        with patch.dict(
            sys.modules,
            {"claude_agent_sdk": mock_sdk, "src.memory": mock_memory},
        ):
            await _execute_job(
                job=job,
                agent_dir="/tmp/test",
                agent_name="me",
                bus=bus,
                chat_id=12345,
            )

        msg = bus._queues["telegram:me"].get_nowait()
        assert msg.chat_id == 12345
        assert msg.metadata.get("message_thread_id") is None
