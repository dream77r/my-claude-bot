"""
Heartbeat — периодическая проверка задач и проактивные уведомления.

Три LLM-вызова:
1. Дешёвый: "Есть задачи в HEARTBEAT.md?" (structured output, да/нет)
2. Полный: выполнение задачи (агентный вызов)
3. Дешёвый: "Стоит ли уведомить пользователя?" (чтобы не спамить)
"""

import asyncio
import logging
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import memory
from . import get_claude_cli_path
from .bus import FleetBus, FleetMessage, MessageType

logger = logging.getLogger(__name__)


async def _call_claude(
    prompt: str,
    model: str = "haiku",
    cwd: str | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """Вызов Claude (простой или агентный)."""
    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        cli_path=get_claude_cli_path(),
    )
    if cwd:
        options.cwd = cwd
    if allowed_tools:
        options.allowed_tools = allowed_tools

    result_text = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    result_text += block.text
        elif isinstance(msg, ResultMessage):
            if msg.result and not result_text:
                result_text = msg.result

    return result_text


async def check_heartbeat(agent_dir: str) -> dict:
    """
    Проверить HEARTBEAT.md и выполнить задачи если есть.

    Returns:
        dict: has_tasks, task_result, should_notify, notification
    """
    result = {
        "has_tasks": False,
        "task_result": "",
        "should_notify": False,
        "notification": "",
    }

    memory_path = memory.get_memory_path(agent_dir)
    heartbeat_path = Path(agent_dir) / "HEARTBEAT.md"

    if not heartbeat_path.exists():
        return result

    content = heartbeat_path.read_text(encoding="utf-8").strip()
    if not content:
        return result

    # ── Шаг 1: Дешёвый вызов — есть ли задачи? ──
    check_prompt = (
        f"Вот содержимое файла HEARTBEAT.md:\n\n{content}\n\n"
        "Есть ли здесь задачи, которые нужно выполнить прямо сейчас?\n"
        "Ответь СТРОГО одним словом: YES или NO"
    )

    try:
        answer = await _call_claude(check_prompt, model="haiku")
    except Exception as e:
        logger.error(f"Heartbeat check error: {e}")
        return result

    if "YES" not in answer.upper():
        logger.debug("Heartbeat: задач нет")
        return result

    result["has_tasks"] = True
    logger.info("Heartbeat: обнаружены задачи, выполняю")

    # ── Шаг 2: Полный агентный вызов ──
    task_prompt = (
        f"Вот содержимое файла HEARTBEAT.md:\n\n{content}\n\n"
        "Выполни задачи, которые описаны в этом файле.\n"
        "После выполнения — обнови HEARTBEAT.md: "
        "отметь выполненные задачи или удали их.\n"
        "Верни краткий отчёт о выполненном."
    )

    try:
        task_result = await _call_claude(
            task_prompt,
            model="sonnet",
            cwd=str(memory_path),
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebSearch", "WebFetch"],
        )
        result["task_result"] = task_result
    except Exception as e:
        logger.error(f"Heartbeat task error: {e}")
        return result

    # ── Шаг 3: Дешёвый вызов — стоит ли уведомлять? ──
    eval_prompt = (
        f"Агент выполнил фоновую задачу. Вот результат:\n\n{task_result[:1000]}\n\n"
        "Стоит ли уведомить пользователя об этом результате?\n"
        "Уведомляй только если:\n"
        "- Результат важный или срочный\n"
        "- Пользователь ждёт этот результат\n"
        "- Обнаружена проблема, требующая внимания\n\n"
        "НЕ уведомляй о рутинных операциях.\n"
        "Ответь СТРОГО: YES или NO"
    )

    try:
        should_notify = await _call_claude(eval_prompt, model="haiku")
    except Exception as e:
        logger.error(f"Heartbeat eval error: {e}")
        return result

    if "YES" in should_notify.upper():
        result["should_notify"] = True
        # Сформировать краткое уведомление
        notify_prompt = (
            f"Сформулируй краткое (1-3 предложения) уведомление для пользователя "
            f"на основе результата фоновой задачи:\n\n{task_result[:1000]}"
        )
        try:
            result["notification"] = await _call_claude(notify_prompt, model="haiku")
        except Exception:
            result["notification"] = task_result[:300]

    # Git commit
    memory.git_commit(agent_dir, "Heartbeat task completed")

    return result


async def heartbeat_loop(
    agent_dir: str,
    agent_name: str,
    bus: FleetBus | None = None,
    chat_id: int = 0,
    interval_minutes: float = 30.0,
) -> None:
    """
    Бесконечный цикл Heartbeat.

    Args:
        agent_dir: путь к директории агента
        agent_name: имя агента
        bus: шина сообщений для отправки уведомлений
        chat_id: ID чата для уведомлений (если нет bus — не используется)
        interval_minutes: интервал проверки
    """
    interval_seconds = interval_minutes * 60
    logger.info(
        f"Heartbeat loop запущен для '{agent_name}', "
        f"интервал: {interval_minutes} мин"
    )

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            result = await check_heartbeat(agent_dir)

            if result["should_notify"] and bus and chat_id:
                msg = FleetMessage(
                    source=f"agent:{agent_name}",
                    target=f"telegram:{agent_name}",
                    content=f"[Heartbeat] {result['notification']}",
                    msg_type=MessageType.OUTBOUND,
                    chat_id=chat_id,
                )
                await bus.publish(msg)
                logger.info(f"Heartbeat: уведомление отправлено в чат {chat_id}")

        except asyncio.CancelledError:
            logger.info("Heartbeat loop остановлен")
            break
        except Exception as e:
            logger.error(f"Heartbeat loop error: {e}")
