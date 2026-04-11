"""
Audit Logging — структурированный аудит-лог.

Логирует каждый tool call, решения маршрутизации, блокировки guard/ssrf.
Хранение: memory/stats/audit.jsonl — одна строка на событие.

OWASP AI Agent Security: "Log every tool call with redacted parameters,
every decision point, anomaly detection."
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from . import memory

logger = logging.getLogger(__name__)

AUDIT_FILE = "stats/audit.jsonl"


def _audit_path(agent_dir: str) -> Path:
    return memory.get_memory_path(agent_dir) / AUDIT_FILE


def log_event(
    agent_dir: str,
    event_type: str,
    agent_name: str = "",
    **kwargs,
) -> None:
    """
    Записать событие в аудит-лог.

    Args:
        agent_dir: директория агента
        event_type: тип события (tool_call, guard_block, ssrf_block, routing, etc.)
        agent_name: имя агента
        **kwargs: дополнительные поля
    """
    path = _audit_path(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": datetime.now().isoformat(),
        "type": event_type,
        "agent": agent_name,
    }

    # Редактирование чувствительных полей
    for key, value in kwargs.items():
        if isinstance(value, str) and len(value) > 300:
            entry[key] = value[:300] + "..."
        elif isinstance(value, dict):
            # Редактировать значения длинных полей в dict
            entry[key] = _redact_dict(value)
        else:
            entry[key] = value

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.error(f"Audit log error: {e}")


def _redact_dict(d: dict, max_value_len: int = 200) -> dict:
    """Сократить длинные значения в dict."""
    result = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > max_value_len:
            result[k] = v[:max_value_len] + "..."
        else:
            result[k] = v
    return result


def get_recent(agent_dir: str, limit: int = 50) -> list[dict]:
    """Получить последние N записей аудит-лога."""
    path = _audit_path(agent_dir)
    if not path.exists():
        return []

    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        logger.error(f"Audit read error: {e}")

    return entries[-limit:]


def make_audit_hook(agent_dir: str):
    """
    Создать on_tool_use хук для аудит-логирования.

    Логирует каждый tool call с редактированными параметрами.
    Также логирует блокировки от guard и ssrf.
    """
    from .hooks import HookContext

    async def _audit_hook(ctx: HookContext) -> HookContext:
        tool_name = ctx.data.get("tool_name", "")
        tool_input = ctx.data.get("tool_input", {})

        # Логировать tool call
        log_event(
            agent_dir,
            event_type="tool_call",
            agent_name=ctx.agent_name,
            tool=tool_name,
            input=tool_input,
        )

        # Логировать блокировки
        if ctx.data.get("guard_blocked"):
            log_event(
                agent_dir,
                event_type="guard_block",
                agent_name=ctx.agent_name,
                tool=tool_name,
                reason=ctx.data.get("guard_reason", ""),
            )

        if ctx.data.get("ssrf_blocked"):
            log_event(
                agent_dir,
                event_type="ssrf_block",
                agent_name=ctx.agent_name,
                tool=tool_name,
                reason=ctx.data.get("ssrf_reason", ""),
                url=tool_input.get("url", ""),
            )

        if ctx.data.get("ssrf_warning"):
            log_event(
                agent_dir,
                event_type="ssrf_warning",
                agent_name=ctx.agent_name,
                tool=tool_name,
                urls=ctx.data["ssrf_warning"],
            )

        return ctx

    return _audit_hook
