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
  - name: "inventory_last_day"
    schedule: "0 10 L * *"       # Последний день месяца, 10:00
    prompt: "Напомни про инвентаризацию..."
    model: "haiku"
    notify: true
    chat_id: -1001234567890      # Отчёт уходит в группу, а не в личку
    thread_id: 42                # Опционально: конкретный топик группы
```

День месяца поддерживает `L` (последний день месяца) и `L-N` (за N дней
до последнего): `L-1` — накануне последнего дня. Число дней в месяце
вычисляется на лету, поэтому правило одинаково корректно для 28/29/30/31.
"""

import asyncio
import calendar
import logging
import re
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
    # Куда слать результат. None → дефолтный чат агента (личка владельца).
    # Явное значение перекрывает его: группа, канал, конкретный топик.
    chat_id: int | None = None
    thread_id: int | None = None


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


# `L` (последний день месяца) и `L-N` (за N дней до последнего).
_LAST_DAY_RE = re.compile(r"^L(?:-(\d+))?$", re.IGNORECASE)


def resolve_last_day_token(token: str, now: datetime) -> int | None:
    """Развернуть `L` / `L-N` в номер дня месяца для даты `now`.

    `L` — последний день месяца, `L-1` — накануне последнего.
    Возвращает None, если токен не в L-формате (обычное число/диапазон).
    """
    match = _LAST_DAY_RE.match(token.strip())
    if match is None:
        return None
    offset = int(match.group(1) or 0)
    last_day = calendar.monthrange(now.year, now.month)[1]
    return last_day - offset


def parse_dom_field(field: str, now: datetime) -> bool:
    """Поле day-of-month с поддержкой `L` / `L-N`.

    Без `L` поведение полностью совпадает с parse_cron_field.
    L-токены смешиваются со списком: "1,15,L", "L,L-1".
    """
    field = field.strip()
    if "l" not in field.lower():
        return parse_cron_field(field, now.day, 31)

    for token in field.split(","):
        day = resolve_last_day_token(token, now)
        if day is None:
            # Обычный токен в списке рядом с L — например "1,L"
            if parse_cron_field(token.strip(), now.day, 31):
                return True
            continue
        # L-N может уйти за начало месяца (L-40) — такой день не наступает
        if day >= 1 and now.day == day:
            return True

    return False


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

    try:
        return (
            parse_cron_field(minute, now.minute, 59)
            and parse_cron_field(hour, now.hour, 23)
            and parse_dom_field(day, now)
            and parse_cron_field(month, now.month, 12)
            and parse_cron_field(weekday, now.isoweekday() % 7, 6)
            # isoweekday: Mon=1..Sun=7, cron: Sun=0..Sat=6
        )
    except ValueError as e:
        # Нечисловой мусор в поле ("0 9 abc * *"). Раньше ValueError
        # вылетал в вызывающий цикл и ронял проверку остальных задач.
        logger.warning(f"Невалидный cron: '{schedule}' ({e})")
        return False


def _optional_int(value: object, job_name: str, field: str) -> int | None:
    """Привести опциональное поле к int. Мусор → None + warning."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            f"Cron '{job_name}': поле {field}={value!r} не int, игнорирую"
        )
        return None


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
                chat_id=_optional_int(item.get("chat_id"), item["name"], "chat_id"),
                thread_id=_optional_int(
                    item.get("thread_id"), item["name"], "thread_id"
                ),
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
    notify_agent_name: str | None = None,
) -> None:
    """Выполнить одну cron-задачу.

    Args:
        notify_agent_name: имя агента для маршрутизации уведомлений.
            Если задан — сообщение отправляется в telegram:{notify_agent_name},
            иначе — в telegram:{agent_name}. Используется для worker-агентов,
            у которых нет прямого chat_id с пользователем: уведомление
            доставляется через telegram-бота мастер-агента.
            Исключение — задача с явным chat_id: она всегда уходит через
            собственного бота агента (см. ниже).
    """
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
        # Явный chat_id задачи перекрывает дефолтный чат агента — так
        # отчёт уходит в конкретную группу, а не в личку владельца.
        target_chat_id = job.chat_id if job.chat_id is not None else chat_id
        # Каким ботом слать.
        #  • Без chat_id у задачи: worker-агенты не имеют прямого чата с
        #    пользователем (chat_id=0), поэтому уведомление уходит через
        #    бота мастера — только у него есть личка с владельцем.
        #  • С явным chat_id: слать надо СВОИМ ботом. В группу/канал
        #    добавляют бота самого агента, а мастер-бот там обычно не
        #    состоит — Telegram ответит "chat not found", и напоминание
        #    молча потеряется. Своего бота и адресует конфиг задачи.
        if job.chat_id is not None:
            target_agent = agent_name
        else:
            target_agent = notify_agent_name or agent_name
        if target_chat_id == 0:
            logger.warning(
                f"Cron '{job.name}': chat_id=0, уведомление не будет доставлено. "
                f"Укажите chat_id у задачи или FOUNDER_TELEGRAM_ID в .env"
            )
        metadata = {}
        if job.thread_id is not None:
            metadata["message_thread_id"] = job.thread_id
        notification = FleetMessage(
            source=f"agent:{agent_name}",
            target=f"telegram:{target_agent}",
            content=f"[{job.name}]\n\n{result_text}",
            msg_type=MessageType.OUTBOUND,
            chat_id=target_chat_id,
            metadata=metadata,
        )
        await bus.publish(notification)
        logger.info(
            f"Cron '{job.name}': уведомление → чат {target_chat_id} "
            f"через бота '{target_agent}'"
        )


async def cron_loop(
    config: dict,
    agent_dir: str,
    agent_name: str,
    bus: FleetBus | None = None,
    chat_id: int = 0,
    notify_agent_name: str | None = None,
) -> None:
    """
    Бесконечный цикл проверки cron-задач каждую минуту.

    Args:
        config: полный конфиг агента (из agent.yaml)
        agent_dir: путь к директории агента
        agent_name: имя агента
        bus: шина сообщений
        chat_id: ID чата для уведомлений
        notify_agent_name: имя агента для маршрутизации уведомлений.
            Для worker-агентов задайте имя мастер-агента, чтобы уведомления
            отправлялись через его telegram-бота (у worker chat_id=0).
            На задачи с явным chat_id не влияет — те идут своим ботом.
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
                        _execute_job(
                            job, agent_dir, agent_name, bus, chat_id,
                            notify_agent_name,
                        ),
                        name=f"cron:{agent_name}:{job.name}",
                    )

        except asyncio.CancelledError:
            logger.info(f"Cron loop '{agent_name}' остановлен")
            break
        except Exception as e:
            logger.error(f"Cron loop error: {e}")
