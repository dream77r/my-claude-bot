"""
Cron — периодические задачи агентов.

Расширение Heartbeat: несколько задач с разными расписаниями.
Каждая задача — промпт для Claude, выполняемый по cron-выражению.

Конфиг в agent.yaml:
```yaml
cron:
  - name: "weekly_summary"
    schedule: "0 9 * * 1"        # Пн 9:00
    prompt: "Сделай сводку за неделю..."
    model: "sonnet"
    notify: true
  - name: "daily_digest"
    schedule: "0 21 * * *"       # Каждый день 21:00
    prompt: "Сделай резюме дня..."
    model: "haiku"
    notify: true
```
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from . import get_claude_cli_path
from .bus import FleetBus, FleetMessage, MessageType
from .task_utils import spawn_supervised

logger = logging.getLogger(__name__)


@dataclass
class CronJob:
    """Одна cron-задача."""
    name: str
    schedule: str            # cron expression: "min hour day month weekday"
    prompt: str
    model: str = "sonnet"
    notify: bool = True
    allowed_tools: list[str] | None = None


def parse_cron_field(field: str, current: int, max_val: int) -> bool:
    """
    Проверить совпадает ли текущее значение с cron-полем.

    Поддерживает: *, N, */N, N-M, N,M,K
    """
    if field == "*":
        return True

    # */N — каждые N
    if field.startswith("*/"):
        step = int(field[2:])
        return current % step == 0

    # N-M — диапазон
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= current <= int(end)

    # N,M,K — список
    if "," in field:
        values = [int(v) for v in field.split(",")]
        return current in values

    # Точное значение
    return current == int(field)


def should_run(schedule: str, now: datetime | None = None) -> bool:
    """
    Проверить должна ли задача запуститься в текущую минуту.

    Args:
        schedule: cron expression "min hour day month weekday"
        now: текущее время (для тестов)
    """
    if now is None:
        now = datetime.now()

    parts = schedule.strip().split()
    if len(parts) != 5:
        logger.warning(f"Невалидный cron: '{schedule}' (нужно 5 полей)")
        return False

    minute, hour, day, month, weekday = parts

    return (
        parse_cron_field(minute, now.minute, 59)
        and parse_cron_field(hour, now.hour, 23)
        and parse_cron_field(day, now.day, 31)
        and parse_cron_field(month, now.month, 12)
        and parse_cron_field(weekday, now.isoweekday() % 7, 6)
        # isoweekday: Mon=1..Sun=7, cron: Sun=0..Sat=6
    )


def load_cron_jobs(config: dict) -> list[CronJob]:
    """Загрузить cron-задачи из конфига агента."""
    jobs = []
    for item in config.get("cron", []):
        try:
            job = CronJob(
                name=item["name"],
                schedule=item["schedule"],
                prompt=item["prompt"],
                model=item.get("model", "sonnet"),
                notify=item.get("notify", True),
                allowed_tools=item.get("allowed_tools"),
            )
            jobs.append(job)
        except KeyError as e:
            logger.warning(f"Пропущена cron-задача: отсутствует поле {e}")
    return jobs


async def _execute_job(
    job: CronJob,
    agent_dir: str,
    agent_name: str,
    bus: FleetBus | None = None,
    chat_id: int = 0,
) -> None:
    """Выполнить одну cron-задачу."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )
    from . import memory

    logger.info(f"Cron '{job.name}' запущен для '{agent_name}'")

    memory_path = memory.get_memory_path(agent_dir)

    options = ClaudeAgentOptions(
        model=job.model,
        permission_mode="bypassPermissions",
        cli_path=get_claude_cli_path(),
        cwd=str(memory_path),
    )
    if job.allowed_tools:
        options.allowed_tools = job.allowed_tools

    result_text = ""
    try:
        async for msg in query(prompt=job.prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text
            elif isinstance(msg, ResultMessage):
                if msg.result and not result_text:
                    result_text = msg.result
    except Exception as e:
        logger.error(f"Cron '{job.name}' error: {e}")
        return

    logger.info(f"Cron '{job.name}' завершён, ответ: {len(result_text)} символов")

    # Git commit
    memory.git_commit(agent_dir, f"Cron: {job.name}")

    # Уведомить если нужно
    if job.notify and bus and result_text:
        notification = FleetMessage(
            source=f"agent:{agent_name}",
            target=f"telegram:{agent_name}",
            content=f"[{job.name}]\n\n{result_text}",
            msg_type=MessageType.OUTBOUND,
            chat_id=chat_id,
        )
        await bus.publish(notification)


async def cron_loop(
    config: dict,
    agent_dir: str,
    agent_name: str,
    bus: FleetBus | None = None,
    chat_id: int = 0,
) -> None:
    """
    Бесконечный цикл проверки cron-задач каждую минуту.

    Args:
        config: полный конфиг агента (из agent.yaml)
        agent_dir: путь к директории агента
        agent_name: имя агента
        bus: шина сообщений
        chat_id: ID чата для уведомлений
    """
    jobs = load_cron_jobs(config)
    if not jobs:
        return

    logger.info(
        f"Cron loop запущен для '{agent_name}': "
        f"{len(jobs)} задач ({', '.join(j.name for j in jobs)})"
    )

    while True:
        try:
            # Спать до начала следующей минуты
            now = time.time()
            sleep_seconds = 60 - (now % 60)
            await asyncio.sleep(sleep_seconds)

            current = datetime.now()
            for job in jobs:
                if should_run(job.schedule, current):
                    # Запустить в отдельной задаче (не блокируем цикл).
                    # spawn_supervised: keep strong ref + log exceptions,
                    # иначе провал cron-джобы уходит в тишину.
                    spawn_supervised(
                        _execute_job(job, agent_dir, agent_name, bus, chat_id),
                        name=f"cron:{agent_name}:{job.name}",
                    )

        except asyncio.CancelledError:
            logger.info(f"Cron loop '{agent_name}' остановлен")
            break
        except Exception as e:
            logger.error(f"Cron loop error: {e}")
