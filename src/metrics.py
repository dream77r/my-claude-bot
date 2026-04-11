"""
Metrics — трекинг использования и стоимости.

Логирует каждый LLM-вызов: модель, latency, tool calls, timestamps.
Предоставляет статистику через /stats команду.
Интегрируется через after_call хук.

Хранение: memory/stats/usage.jsonl — одна строка на вызов.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import memory

logger = logging.getLogger(__name__)

# Файл метрик
USAGE_FILE = "stats/usage.jsonl"

# Дневной лимит вызовов (защита от зацикливания)
DEFAULT_DAILY_LIMIT = 500


def _usage_path(agent_dir: str) -> Path:
    """Путь к файлу метрик."""
    return memory.get_memory_path(agent_dir) / USAGE_FILE


def log_call(
    agent_dir: str,
    model: str,
    latency_s: float,
    tool_calls: int = 0,
    prompt_chars: int = 0,
    response_chars: int = 0,
    error: str | None = None,
) -> None:
    """
    Записать метрику LLM-вызова.

    Args:
        agent_dir: директория агента
        model: использованная модель (haiku/sonnet/opus)
        latency_s: время вызова в секундах
        tool_calls: количество tool use в ответе
        prompt_chars: длина промпта в символах
        response_chars: длина ответа в символах
        error: описание ошибки (если была)
    """
    path = _usage_path(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": datetime.now().isoformat(),
        "model": model,
        "latency_s": round(latency_s, 2),
        "tool_calls": tool_calls,
        "prompt_chars": prompt_chars,
        "response_chars": response_chars,
    }
    if error:
        entry["error"] = error

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Metrics log error: {e}")


def get_stats(agent_dir: str, days: int = 1) -> dict:
    """
    Получить статистику за N дней.

    Returns:
        dict с ключами: total_calls, avg_latency, models, tool_calls,
        total_prompt_chars, total_response_chars, errors, period
    """
    path = _usage_path(agent_dir)
    if not path.exists():
        return {
            "total_calls": 0,
            "avg_latency": 0,
            "models": {},
            "tool_calls": 0,
            "total_prompt_chars": 0,
            "total_response_chars": 0,
            "errors": 0,
            "period": f"{days}d",
        }

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    entries = []

    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("ts", "") >= cutoff:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error(f"Metrics read error: {e}")

    if not entries:
        return {
            "total_calls": 0,
            "avg_latency": 0,
            "models": {},
            "tool_calls": 0,
            "total_prompt_chars": 0,
            "total_response_chars": 0,
            "errors": 0,
            "period": f"{days}d",
        }

    total = len(entries)
    latencies = [e.get("latency_s", 0) for e in entries]
    models: dict[str, int] = {}
    for e in entries:
        m = e.get("model", "unknown")
        models[m] = models.get(m, 0) + 1

    return {
        "total_calls": total,
        "avg_latency": round(sum(latencies) / len(latencies), 1),
        "max_latency": round(max(latencies), 1),
        "models": models,
        "tool_calls": sum(e.get("tool_calls", 0) for e in entries),
        "total_prompt_chars": sum(e.get("prompt_chars", 0) for e in entries),
        "total_response_chars": sum(e.get("response_chars", 0) for e in entries),
        "errors": sum(1 for e in entries if e.get("error")),
        "period": f"{days}d",
    }


def format_stats(stats: dict) -> str:
    """Отформатировать статистику для Telegram."""
    if stats["total_calls"] == 0:
        return "Нет данных за выбранный период."

    lines = [
        f"📊 Статистика за {stats['period']}",
        "",
        f"Вызовов: {stats['total_calls']}",
        f"Ср. latency: {stats['avg_latency']}с",
        f"Макс. latency: {stats.get('max_latency', 'N/A')}с",
        f"Tool calls: {stats['tool_calls']}",
        f"Ошибок: {stats['errors']}",
        "",
        f"Символов prompt: {stats['total_prompt_chars']:,}",
        f"Символов response: {stats['total_response_chars']:,}",
    ]

    if stats["models"]:
        lines.append("")
        lines.append("Модели:")
        for model, count in sorted(
            stats["models"].items(), key=lambda x: -x[1]
        ):
            lines.append(f"  {model}: {count}")

    return "\n".join(lines)


def check_daily_limit(agent_dir: str, limit: int = DEFAULT_DAILY_LIMIT) -> tuple[bool, int]:
    """
    Проверить дневной лимит вызовов.

    Returns:
        (within_limit, current_count)
    """
    stats = get_stats(agent_dir, days=1)
    count = stats["total_calls"]
    return count < limit, count


def make_metrics_hook(agent_dir: str, model: str = "sonnet"):
    """
    Создать after_call хук для автоматического логирования метрик.

    Трекает latency, размер промпта/ответа, количество tool calls.
    """
    from .hooks import HookContext

    _state: dict = {"start_time": 0, "tool_calls": 0}

    async def _before_hook(ctx: HookContext) -> HookContext:
        """Запомнить время начала вызова."""
        _state["start_time"] = time.monotonic()
        _state["tool_calls"] = 0
        return ctx

    async def _tool_hook(ctx: HookContext) -> HookContext:
        """Считать tool calls."""
        _state["tool_calls"] = _state.get("tool_calls", 0) + 1
        return ctx

    async def _after_hook(ctx: HookContext) -> HookContext:
        """Записать метрику после вызова."""
        start = _state.get("start_time", 0)
        latency = time.monotonic() - start if start else 0

        message = ctx.data.get("message", "")
        response = ctx.data.get("response", "")

        log_call(
            agent_dir,
            model=model,
            latency_s=latency,
            tool_calls=_state.get("tool_calls", 0),
            prompt_chars=len(message),
            response_chars=len(response),
        )

        return ctx

    async def _error_hook(ctx: HookContext) -> HookContext:
        """Записать ошибку."""
        start = _state.get("start_time", 0)
        latency = time.monotonic() - start if start else 0
        error = ctx.data.get("error", "")

        log_call(
            agent_dir,
            model=model,
            latency_s=latency,
            error=str(error)[:200],
        )

        return ctx

    return _before_hook, _tool_hook, _after_hook, _error_hook
