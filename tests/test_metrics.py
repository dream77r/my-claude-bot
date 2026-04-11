"""Тесты для Metrics — трекинг использования."""

import json
from pathlib import Path

import pytest

from src.metrics import (
    check_daily_limit,
    format_stats,
    get_stats,
    log_call,
    make_metrics_hook,
)


@pytest.fixture
def agent_dir(tmp_path):
    """Создать временную директорию агента с memory/stats/."""
    d = tmp_path / "agents" / "test"
    (d / "memory" / "stats").mkdir(parents=True)
    return str(d)


class TestLogCall:
    """Логирование вызовов."""

    def test_creates_file(self, agent_dir):
        log_call(agent_dir, model="sonnet", latency_s=1.5)
        path = Path(agent_dir) / "memory" / "stats" / "usage.jsonl"
        assert path.exists()

    def test_writes_json_line(self, agent_dir):
        log_call(agent_dir, model="haiku", latency_s=0.3, tool_calls=2)
        path = Path(agent_dir) / "memory" / "stats" / "usage.jsonl"
        line = path.read_text().strip()
        data = json.loads(line)
        assert data["model"] == "haiku"
        assert data["latency_s"] == 0.3
        assert data["tool_calls"] == 2

    def test_appends_multiple(self, agent_dir):
        log_call(agent_dir, model="haiku", latency_s=0.1)
        log_call(agent_dir, model="sonnet", latency_s=1.0)
        log_call(agent_dir, model="opus", latency_s=5.0)
        path = Path(agent_dir) / "memory" / "stats" / "usage.jsonl"
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_logs_error(self, agent_dir):
        log_call(agent_dir, model="sonnet", latency_s=0, error="timeout")
        path = Path(agent_dir) / "memory" / "stats" / "usage.jsonl"
        data = json.loads(path.read_text().strip())
        assert data["error"] == "timeout"

    def test_logs_char_counts(self, agent_dir):
        log_call(
            agent_dir,
            model="sonnet",
            latency_s=2.0,
            prompt_chars=1000,
            response_chars=500,
        )
        path = Path(agent_dir) / "memory" / "stats" / "usage.jsonl"
        data = json.loads(path.read_text().strip())
        assert data["prompt_chars"] == 1000
        assert data["response_chars"] == 500


class TestGetStats:
    """Получение статистики."""

    def test_empty_stats(self, agent_dir):
        stats = get_stats(agent_dir)
        assert stats["total_calls"] == 0

    def test_counts_calls(self, agent_dir):
        for _ in range(5):
            log_call(agent_dir, model="sonnet", latency_s=1.0)
        stats = get_stats(agent_dir, days=1)
        assert stats["total_calls"] == 5

    def test_avg_latency(self, agent_dir):
        log_call(agent_dir, model="sonnet", latency_s=1.0)
        log_call(agent_dir, model="sonnet", latency_s=3.0)
        stats = get_stats(agent_dir)
        assert stats["avg_latency"] == 2.0

    def test_model_breakdown(self, agent_dir):
        log_call(agent_dir, model="haiku", latency_s=0.1)
        log_call(agent_dir, model="haiku", latency_s=0.2)
        log_call(agent_dir, model="sonnet", latency_s=1.0)
        stats = get_stats(agent_dir)
        assert stats["models"]["haiku"] == 2
        assert stats["models"]["sonnet"] == 1

    def test_error_count(self, agent_dir):
        log_call(agent_dir, model="sonnet", latency_s=1.0)
        log_call(agent_dir, model="sonnet", latency_s=0, error="timeout")
        stats = get_stats(agent_dir)
        assert stats["errors"] == 1

    def test_tool_calls_sum(self, agent_dir):
        log_call(agent_dir, model="sonnet", latency_s=1.0, tool_calls=3)
        log_call(agent_dir, model="sonnet", latency_s=2.0, tool_calls=5)
        stats = get_stats(agent_dir)
        assert stats["tool_calls"] == 8


class TestFormatStats:
    """Форматирование статистики."""

    def test_empty_stats(self):
        text = format_stats({"total_calls": 0})
        assert "Нет данных" in text

    def test_with_data(self):
        text = format_stats({
            "total_calls": 10,
            "avg_latency": 1.5,
            "max_latency": 5.0,
            "models": {"sonnet": 8, "haiku": 2},
            "tool_calls": 15,
            "total_prompt_chars": 10000,
            "total_response_chars": 5000,
            "errors": 1,
            "period": "1d",
        })
        assert "10" in text
        assert "1.5" in text
        assert "sonnet" in text


class TestDailyLimit:
    """Проверка дневного лимита."""

    def test_under_limit(self, agent_dir):
        log_call(agent_dir, model="sonnet", latency_s=1.0)
        ok, count = check_daily_limit(agent_dir, limit=10)
        assert ok
        assert count == 1

    def test_at_limit(self, agent_dir):
        for _ in range(10):
            log_call(agent_dir, model="sonnet", latency_s=0.1)
        ok, count = check_daily_limit(agent_dir, limit=10)
        assert not ok
        assert count == 10

    def test_no_data(self, agent_dir):
        ok, count = check_daily_limit(agent_dir)
        assert ok
        assert count == 0


@pytest.mark.asyncio
class TestMetricsHook:
    """Тесты хуков метрик."""

    async def test_before_and_after(self, agent_dir):
        from src.hooks import HookContext

        before_fn, tool_fn, after_fn, error_fn = make_metrics_hook(agent_dir)

        # Before
        ctx = HookContext(
            event="before_call",
            agent_name="test",
            data={"message": "test prompt"},
        )
        await before_fn(ctx)

        # After
        ctx = HookContext(
            event="after_call",
            agent_name="test",
            data={"message": "test prompt", "response": "test response"},
        )
        await after_fn(ctx)

        # Проверить что запись появилась
        stats = get_stats(agent_dir)
        assert stats["total_calls"] == 1
        assert stats["total_prompt_chars"] > 0

    async def test_tool_counting(self, agent_dir):
        from src.hooks import HookContext

        before_fn, tool_fn, after_fn, error_fn = make_metrics_hook(agent_dir)

        await before_fn(HookContext(
            event="before_call", agent_name="test",
            data={"message": "x"},
        ))

        # 3 tool calls
        for _ in range(3):
            await tool_fn(HookContext(
                event="on_tool_use", agent_name="test",
                data={"tool_name": "Read"},
            ))

        await after_fn(HookContext(
            event="after_call", agent_name="test",
            data={"message": "x", "response": "y"},
        ))

        stats = get_stats(agent_dir)
        assert stats["tool_calls"] == 3
