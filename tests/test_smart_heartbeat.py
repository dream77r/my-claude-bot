"""Тесты для smart_heartbeat.py — SmartTrigger + SmartHeartbeat."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.smart_heartbeat import SmartHeartbeat, SmartTrigger


# ── SmartTrigger ──


class TestSmartTrigger:
    def test_init_minimal(self):
        config = {
            "name": "test",
            "schedule": "0 9 * * *",
            "prompt": "Do something",
        }
        trigger = SmartTrigger(config)
        assert trigger.name == "test"
        assert trigger.schedule == "0 9 * * *"
        assert trigger.prompt == "Do something"
        assert trigger.model == "haiku"
        assert trigger.notify == "auto"
        assert trigger.allowed_tools == ["Read", "Write", "Glob", "Grep"]

    def test_init_full(self):
        config = {
            "name": "morning",
            "schedule": "0 9 * * *",
            "prompt": "Briefing",
            "model": "sonnet",
            "notify": True,
            "allowed_tools": ["Read", "Grep"],
        }
        trigger = SmartTrigger(config)
        assert trigger.model == "sonnet"
        assert trigger.notify is True
        assert trigger.allowed_tools == ["Read", "Grep"]

    def test_should_run_match(self):
        trigger = SmartTrigger({
            "name": "t", "schedule": "0 9 * * *", "prompt": "p",
        })
        now = datetime(2026, 4, 11, 9, 0)
        assert trigger.should_run(now) is True

    def test_should_run_no_match(self):
        trigger = SmartTrigger({
            "name": "t", "schedule": "0 9 * * *", "prompt": "p",
        })
        now = datetime(2026, 4, 11, 10, 0)
        assert trigger.should_run(now) is False

    def test_should_run_every_4_hours(self):
        trigger = SmartTrigger({
            "name": "t", "schedule": "0 */4 * * *", "prompt": "p",
        })
        assert trigger.should_run(datetime(2026, 4, 11, 0, 0)) is True
        assert trigger.should_run(datetime(2026, 4, 11, 4, 0)) is True
        assert trigger.should_run(datetime(2026, 4, 11, 8, 0)) is True
        assert trigger.should_run(datetime(2026, 4, 11, 3, 0)) is False


# ── SmartHeartbeat ──


class TestSmartHeartbeatInit:
    def test_init_with_triggers(self):
        config = {
            "enabled": True,
            "interval_minutes": 30,
            "triggers": [
                {"name": "t1", "schedule": "0 9 * * *", "prompt": "p1"},
                {"name": "t2", "schedule": "0 21 * * *", "prompt": "p2"},
            ],
        }
        hb = SmartHeartbeat("/tmp/agent", "test", config)
        assert len(hb.triggers) == 2
        assert hb.triggers[0].name == "t1"
        assert hb.triggers[1].name == "t2"
        assert hb.legacy_interval == 30
        assert hb.legacy_enabled is True

    def test_init_no_triggers(self):
        config = {"enabled": True, "interval_minutes": 15}
        hb = SmartHeartbeat("/tmp/agent", "test", config)
        assert len(hb.triggers) == 0
        assert hb.legacy_interval == 15

    def test_init_defaults(self):
        hb = SmartHeartbeat("/tmp/agent", "test", {})
        assert hb.legacy_interval == 30
        assert hb.legacy_enabled is True
        assert len(hb.triggers) == 0


class TestSmartHeartbeatDuplicateProtection:
    def test_no_duplicate_trigger_same_minute(self):
        """Триггер не должен запускаться дважды в одну минуту."""
        config = {
            "triggers": [
                {"name": "t1", "schedule": "* * * * *", "prompt": "p"},
            ],
        }
        hb = SmartHeartbeat("/tmp/agent", "test", config)
        now = datetime(2026, 4, 11, 9, 0)
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        trigger_key = f"t1:{minute_key}"

        # Первый раз — нет в _last_run
        assert trigger_key not in hb._last_run

        # Записать как уже запущенный
        hb._last_run[trigger_key] = minute_key

        # Второй раз — есть в _last_run
        assert trigger_key in hb._last_run

    def test_cleanup_old_entries(self):
        """Старые записи должны очищаться."""
        hb = SmartHeartbeat("/tmp/agent", "test", {"triggers": []})

        # Добавить записи за разные часы
        hb._last_run = {
            "t1:2026-04-11 07:00": "2026-04-11 07:00",
            "t1:2026-04-11 08:00": "2026-04-11 08:00",
            "t1:2026-04-11 09:00": "2026-04-11 09:00",
            "t1:2026-04-11 09:30": "2026-04-11 09:30",
        }

        now = datetime(2026, 4, 11, 9, 45)
        hb._cleanup_last_run(now)

        # Остались только записи за текущий час (09:xx)
        assert "t1:2026-04-11 07:00" not in hb._last_run
        assert "t1:2026-04-11 08:00" not in hb._last_run
        assert "t1:2026-04-11 09:00" in hb._last_run
        assert "t1:2026-04-11 09:30" in hb._last_run


class TestSmartHeartbeatNotifyDecision:
    def test_notify_true(self):
        """notify=true — всегда уведомлять."""
        config = {
            "triggers": [
                {"name": "t", "schedule": "0 9 * * *", "prompt": "p",
                 "notify": True},
            ],
        }
        hb = SmartHeartbeat("/tmp/agent", "test", config)
        assert hb.triggers[0].notify is True

    def test_notify_false(self):
        """notify=false — никогда не уведомлять."""
        config = {
            "triggers": [
                {"name": "t", "schedule": "0 9 * * *", "prompt": "p",
                 "notify": False},
            ],
        }
        hb = SmartHeartbeat("/tmp/agent", "test", config)
        assert hb.triggers[0].notify is False

    def test_notify_auto(self):
        """notify='auto' — LLM решает."""
        config = {
            "triggers": [
                {"name": "t", "schedule": "0 9 * * *", "prompt": "p",
                 "notify": "auto"},
            ],
        }
        hb = SmartHeartbeat("/tmp/agent", "test", config)
        assert hb.triggers[0].notify == "auto"

    def test_notify_default_auto(self):
        """По умолчанию notify='auto'."""
        config = {
            "triggers": [
                {"name": "t", "schedule": "0 9 * * *", "prompt": "p"},
            ],
        }
        hb = SmartHeartbeat("/tmp/agent", "test", config)
        assert hb.triggers[0].notify == "auto"


class TestSmartHeartbeatLegacy:
    def test_legacy_heartbeat_timing(self):
        """Legacy heartbeat проверяется по интервалу."""
        config = {"enabled": True, "interval_minutes": 30, "triggers": []}
        hb = SmartHeartbeat("/tmp/agent", "test", config)

        # Первый вызов устанавливает _last_legacy_run
        assert hb._last_legacy_run is None

    def test_legacy_disabled(self):
        """Если enabled=false, legacy не запускается."""
        config = {"enabled": False, "interval_minutes": 30, "triggers": []}
        hb = SmartHeartbeat("/tmp/agent", "test", config)
        assert hb.legacy_enabled is False


class TestSmartHeartbeatLogDaily:
    @patch("src.smart_heartbeat.memory")
    def test_log_to_daily(self, mock_memory):
        """Результат триггера записывается в daily note."""
        hb = SmartHeartbeat("/tmp/agent", "test", {"triggers": []})
        hb._log_to_daily("morning_briefing", "Test result")

        mock_memory.log_message.assert_called_once()
        call_args = mock_memory.log_message.call_args
        assert call_args[1]["role"] == "assistant"
        assert "morning_briefing" in call_args[1]["content"]
        assert "Test result" in call_args[1]["content"]

    @patch("src.smart_heartbeat.memory")
    def test_log_to_daily_error_handled(self, mock_memory):
        """Ошибка записи в daily note не вызывает краш."""
        mock_memory.log_message.side_effect = Exception("Write error")
        hb = SmartHeartbeat("/tmp/agent", "test", {"triggers": []})
        # Не должен выбросить исключение
        hb._log_to_daily("test", "result")
