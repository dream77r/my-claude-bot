"""
Reminders persistence — сохранение напоминаний через CronCreate между перезапусками.

Проблема: SDK-инструмент CronCreate хранит задачи только в RAM Claude-сессии.
При перезапуске бота все пользовательские напоминания теряются.

Решение:
1. on_tool_use хук перехватывает CronCreate/CronDelete и синхронизирует
   memory/reminders/reminders.json.
2. reminders_loop каждую минуту читает JSON и публикует сообщения через bus.

Формат reminders.json (list):
[
  {
    "id": "uuid4",                    # наш ID (для внутреннего tracking)
    "tool_use_id": "toolu_01...",     # block.id из ToolUseBlock — нужен для
                                      # сопоставления с CronDelete (job_id)
    "schedule": "0 9 * * 1",         # cron expression
    "prompt": "Напомни о встрече",   # промпт для агента
    "recurring": true,               # false = разовое, удалить после срабатывания
    "created_at": "2026-04-22T10:00:00"
  }
]

Matching CronDelete: Claude вызывает CronDelete(id=<job_id>), где job_id — это
значение, которое вернул CronCreate. Мы не знаем наперёд что именно вернёт
CronCreate, поэтому при удалении сопоставляем сразу по трём полям:
  - id (наш UUID)
  - tool_use_id (block.id, который Claude Code может использовать как job_id)
  - по самому значению id из CronCreate-ответа (если он хранится как tool_use_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import memory
from .bus import FleetBus, FleetMessage, MessageType
from .cron import should_run
from .hooks import HookContext
from .task_utils import spawn_supervised

logger = logging.getLogger(__name__)

REMINDERS_FILE = "reminders/reminders.json"


# ── Хранилище ──


def _reminders_path(agent_dir: str) -> Path:
    return memory.get_memory_path(agent_dir) / REMINDERS_FILE


def _load_reminders(agent_dir: str) -> list[dict]:
    """Загрузить список напоминаний из JSON. Возвращает [] при любой ошибке."""
    path = _reminders_path(agent_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Reminders: не удалось прочитать {path}: {e}")
        return []


def _save_reminders(agent_dir: str, reminders: list[dict]) -> None:
    """Сохранить список напоминаний в JSON."""
    path = _reminders_path(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(reminders, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _matches_delete_id(reminder: dict, cron_id: str) -> bool:
    """
    Проверить, соответствует ли напоминание ID из CronDelete.

    CronCreate возвращает job_id, который Claude передаёт в CronDelete.
    Мы не знаем формат этого ID, поэтому проверяем оба варианта:
    - наш UUID (на случай если Claude как-то получил его)
    - tool_use_id (block.id) — потенциальный источник job_id в Claude Code
    """
    return reminder.get("id") == cron_id or reminder.get("tool_use_id") == cron_id


# ── Хук персистентности ──


def make_reminders_hook(agent_dir: str):
    """
    Создать on_tool_use хук для персистентности напоминаний.

    Перехватывает:
    - CronCreate → сохраняет напоминание в reminders.json
    - CronDelete → удаляет напоминание из reminders.json

    Args:
        agent_dir: директория агента (путь к папке agents/{name}/)
    """

    async def _hook(ctx: HookContext) -> HookContext:
        tool_name = ctx.data.get("tool_name", "")
        tool_input = ctx.data.get("tool_input", {}) or {}

        if tool_name == "CronCreate":
            # tool_use_id нужен для сопоставления с CronDelete.
            # Передаётся из agent.py через ctx.data["tool_use_id"].
            tool_use_id = ctx.data.get("tool_use_id", "")

            schedule = tool_input.get("cron", "").strip()
            prompt = tool_input.get("prompt", "").strip()

            if not schedule or not prompt:
                logger.warning(
                    "Reminders hook: CronCreate без schedule/prompt, пропускаю"
                )
                return ctx

            reminder: dict = {
                "id": str(uuid.uuid4()),
                "tool_use_id": tool_use_id,
                "schedule": schedule,
                "prompt": prompt,
                "recurring": bool(tool_input.get("recurring", True)),
                "created_at": datetime.now().isoformat(),
            }

            reminders = _load_reminders(agent_dir)
            reminders.append(reminder)
            _save_reminders(agent_dir, reminders)
            logger.info(
                f"Reminders: сохранено напоминание '{reminder['id'][:8]}' "
                f"schedule='{schedule}' recurring={reminder['recurring']}"
            )

        elif tool_name == "CronDelete":
            cron_id = tool_input.get("id", "").strip()
            if not cron_id:
                return ctx

            reminders = _load_reminders(agent_dir)
            before = len(reminders)
            reminders = [
                r for r in reminders if not _matches_delete_id(r, cron_id)
            ]
            deleted = before - len(reminders)

            if deleted:
                _save_reminders(agent_dir, reminders)
                logger.info(
                    f"Reminders: удалено {deleted} напоминаний по id='{cron_id}'"
                )
            else:
                logger.debug(
                    f"Reminders: CronDelete id='{cron_id}' — не найдено в JSON "
                    "(job_id от CronCreate не совпал ни с id ни с tool_use_id)"
                )

        return ctx

    return _hook


# ── Фоновый цикл ──


async def _fire_reminder(
    reminder: dict,
    agent_dir: str,
    agent_name: str,
    bus: FleetBus,
    chat_id: int,
) -> None:
    """Опубликовать напоминание как входящее сообщение агенту через bus."""
    msg = FleetMessage(
        source=f"reminders:{agent_name}",
        target=f"telegram:{agent_name}",
        content=reminder["prompt"],
        msg_type=MessageType.INBOUND,
        chat_id=chat_id,
    )
    await bus.publish(msg)
    logger.info(
        f"Reminders: fired '{reminder['id'][:8]}' для '{agent_name}' "
        f"(schedule='{reminder['schedule']}')"
    )


async def reminders_loop(
    agent_dir: str,
    agent_name: str,
    bus: FleetBus,
    chat_id: int = 0,
) -> None:
    """
    Бесконечный цикл проверки напоминаний каждую минуту.

    Каждую минуту:
    1. Читает memory/reminders/reminders.json
    2. Для каждого напоминания проверяет расписание через should_run()
    3. При совпадении — публикует сообщение агенту через bus
    4. Разовые напоминания (recurring=False) удаляет после срабатывания

    Args:
        agent_dir:  директория агента (agents/{name}/)
        agent_name: имя агента
        bus:        шина сообщений FleetBus
        chat_id:    Telegram chat_id для доставки уведомлений
    """
    logger.info(
        f"Reminders loop запущен для '{agent_name}' (chat_id={chat_id})"
    )

    while True:
        try:
            # Спать до начала следующей минуты (аналогично cron_loop)
            now = time.time()
            sleep_seconds = 60 - (now % 60)
            await asyncio.sleep(sleep_seconds)

            reminders = _load_reminders(agent_dir)
            if not reminders:
                continue

            current = datetime.now()
            to_delete: list[str] = []

            for reminder in reminders:
                schedule = reminder.get("schedule", "")
                if not schedule:
                    continue

                if should_run(schedule, current):
                    spawn_supervised(
                        _fire_reminder(
                            reminder, agent_dir, agent_name, bus, chat_id
                        ),
                        name=f"reminder:{agent_name}:{reminder['id'][:8]}",
                    )
                    # Разовое напоминание — удалить после срабатывания
                    if not reminder.get("recurring", True):
                        to_delete.append(reminder["id"])

            if to_delete:
                remaining = [r for r in reminders if r["id"] not in to_delete]
                _save_reminders(agent_dir, remaining)
                logger.info(
                    f"Reminders: удалено {len(to_delete)} разовых "
                    f"для '{agent_name}'"
                )

        except asyncio.CancelledError:
            logger.info(f"Reminders loop '{agent_name}' остановлен")
            break
        except Exception as e:
            logger.error(f"Reminders loop '{agent_name}' error: {e}", exc_info=True)
