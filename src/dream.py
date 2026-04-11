"""
Dream Memory — фоновая обработка памяти.

Каждые N часов агент "засыпает" и перерабатывает историю:
- Phase 1: Обычный LLM-вызов — извлечение фактов из новых сообщений
- Phase 2: Агентный вызов с tools — хирургическое обновление wiki
- git_commit после каждого цикла
"""

import asyncio
import json
import logging
from datetime import datetime
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
from .input_sanitizer import sanitize_for_dream

logger = logging.getLogger(__name__)

# Файл для трекинга позиции курсора (последняя обработанная запись)
CURSOR_FILE = "sessions/.dream_cursor"


def _get_cursor(agent_dir: str) -> str | None:
    """Прочитать курсор последнего Dream-цикла (ISO timestamp)."""
    cursor_path = memory.get_memory_path(agent_dir) / CURSOR_FILE
    if cursor_path.exists():
        val = cursor_path.read_text(encoding="utf-8").strip()
        return val if val else None
    return None


def _save_cursor(agent_dir: str, timestamp: str) -> None:
    """Сохранить курсор."""
    cursor_path = memory.get_memory_path(agent_dir) / CURSOR_FILE
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(timestamp, encoding="utf-8")


def get_unprocessed_messages(agent_dir: str) -> list[dict]:
    """Получить сообщения после последнего Dream-курсора."""
    cursor = _get_cursor(agent_dir)
    all_msgs = memory.get_recent_messages(agent_dir, limit=500)

    if not cursor:
        return all_msgs

    result = []
    for msg in all_msgs:
        ts = msg.get("timestamp", "")
        if ts > cursor:
            result.append(msg)
    return result


def _load_template(agent_dir: str, template_name: str) -> str:
    """Загрузить промпт-шаблон из templates/."""
    path = Path(agent_dir) / "templates" / template_name
    if path.exists():
        return path.read_text(encoding="utf-8")
    # Fallback: простой шаблон
    logger.warning(f"Шаблон {template_name} не найден, используется fallback")
    if "phase1" in template_name:
        return (
            "Извлеки ключевые факты из этих сообщений:\n\n{conversations}\n\n"
            "Ответь в JSON: {{\"facts\": [...], \"summary\": \"...\"}}"
        )
    return "Обнови wiki на основе этих фактов:\n\n{facts_json}"


async def _call_claude_simple(
    prompt: str,
    model: str = "haiku",
    cwd: str | None = None,
) -> str:
    """Простой (не-агентный) вызов Claude для Phase 1."""
    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        cli_path=get_claude_cli_path(),
    )
    if cwd:
        options.cwd = cwd

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


async def _call_claude_agent(
    prompt: str,
    model: str = "sonnet",
    cwd: str | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """Агентный вызов Claude для Phase 2 (с tools)."""
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


async def dream_cycle(
    agent_dir: str,
    model_phase1: str = "haiku",
    model_phase2: str = "sonnet",
    skill_advisor_config: dict | None = None,
    bus=None,
    agent_name: str = "",
) -> dict:
    """
    Выполнить один Dream-цикл.

    Args:
        skill_advisor_config: конфиг Phase 3 (skill_advisor из agent.yaml)
        bus: FleetBus для отправки предложений мастеру
        agent_name: имя агента (для Phase 3)

    Returns:
        dict с ключами: facts_count, summary, phase1_ok, phase2_ok, phase3_ok
    """
    result = {
        "facts_count": 0,
        "summary": "",
        "phase1_ok": False,
        "phase2_ok": False,
        "phase3_ok": False,
        "skill_suggestions": 0,
    }

    # Получить необработанные сообщения
    messages = get_unprocessed_messages(agent_dir)
    if not messages:
        logger.info("Dream: нет новых сообщений для обработки")
        result["summary"] = "Нет новых сообщений"
        return result

    logger.info(f"Dream: обрабатываю {len(messages)} новых сообщений")

    memory_path = memory.get_memory_path(agent_dir)

    # Подготовить контекст для Phase 1 (с санитизацией для wiki-ingest)
    sanitized_lines = []
    injection_warnings = 0
    for m in messages:
        content = m.get("content", "")[:500]
        cleaned, findings = sanitize_for_dream(content)
        if findings:
            injection_warnings += 1
            cleaned = f"[SANITIZED: {', '.join(findings)}] {cleaned}"
        sanitized_lines.append(
            f"[{m.get('timestamp', '?')}] {m.get('role', '?')}: {cleaned}"
        )
    if injection_warnings:
        logger.warning(
            f"Dream: {injection_warnings} сообщений с подозрительным контентом"
        )
    conversations_text = "\n".join(sanitized_lines)

    profile_text = ""
    profile_path = memory_path / "profile.md"
    if profile_path.exists():
        profile_text = profile_path.read_text(encoding="utf-8")

    index_text = ""
    index_path = memory_path / "index.md"
    if index_path.exists():
        index_text = index_path.read_text(encoding="utf-8")

    # ── Phase 1: Извлечение фактов ──
    template1 = _load_template(agent_dir, "dream_phase1.md")
    prompt1 = template1.format(
        conversations=conversations_text,
        profile=profile_text,
        index=index_text,
    )

    try:
        phase1_response = await _call_claude_simple(
            prompt1, model=model_phase1, cwd=str(memory_path)
        )
        result["phase1_ok"] = True
    except Exception as e:
        logger.error(f"Dream Phase 1 error: {e}")
        return result

    # Парсим JSON из ответа
    facts_json = _extract_json(phase1_response)
    if not facts_json:
        logger.warning("Dream Phase 1: не удалось извлечь JSON из ответа")
        result["summary"] = "Phase 1 не вернула валидный JSON"
        return result

    facts = facts_json.get("facts", [])
    result["facts_count"] = len(facts)
    result["summary"] = facts_json.get("summary", "")

    if not facts:
        logger.info("Dream: новых фактов не обнаружено")
        # Обновить курсор даже если фактов нет
        if messages:
            last_ts = messages[-1].get("timestamp", datetime.now().isoformat())
            _save_cursor(agent_dir, last_ts)
        return result

    # ── Phase 2: Обновление wiki ──
    template2 = _load_template(agent_dir, "dream_phase2.md")
    prompt2 = template2.format(facts_json=json.dumps(facts, ensure_ascii=False, indent=2))

    try:
        await _call_claude_agent(
            prompt2,
            model=model_phase2,
            cwd=str(memory_path),
            allowed_tools=["Read", "Write", "Edit", "Glob"],
        )
        result["phase2_ok"] = True
    except Exception as e:
        logger.error(f"Dream Phase 2 error: {e}")

    # Обновить курсор
    if messages:
        last_ts = messages[-1].get("timestamp", datetime.now().isoformat())
        _save_cursor(agent_dir, last_ts)

    # Git commit
    memory.git_commit(agent_dir, f"Dream cycle: {len(facts)} facts extracted")

    # ── Phase 3: Анализ паттернов и предложение скиллов ──
    if skill_advisor_config and skill_advisor_config.get("enabled", False):
        try:
            from .skill_advisor import (
                analyze_patterns,
                report_to_master,
                store_suggestions,
            )

            sa_model = skill_advisor_config.get("model", "haiku")
            sa_days = skill_advisor_config.get("analysis_days", 7)
            master_name = skill_advisor_config.get("master_name", "me")

            suggestions = await analyze_patterns(
                agent_dir,
                agent_name=agent_name,
                model=sa_model,
                days=sa_days,
            )

            if suggestions:
                # Сохранить локально
                store_suggestions(agent_dir, suggestions)
                result["skill_suggestions"] = len(suggestions)

                # Отправить мастеру
                if bus:
                    await report_to_master(
                        agent_name, suggestions, bus, master_name
                    )

                result["phase3_ok"] = True
                logger.info(
                    f"Dream Phase 3: {len(suggestions)} предложений по скиллам"
                )
            else:
                result["phase3_ok"] = True  # OK но без предложений
        except Exception as e:
            logger.error(f"Dream Phase 3 error: {e}")

    logger.info(
        f"Dream завершён: {len(facts)} фактов, "
        f"phase1={'ok' if result['phase1_ok'] else 'fail'}, "
        f"phase2={'ok' if result['phase2_ok'] else 'fail'}"
        + (
            f", phase3={'ok' if result['phase3_ok'] else 'fail'} "
            f"({result['skill_suggestions']} suggestions)"
            if skill_advisor_config and skill_advisor_config.get("enabled")
            else ""
        )
    )

    return result


def _extract_json(text: str) -> dict | None:
    """Извлечь JSON из текста (может быть в ```json блоке)."""
    import re

    # Попробовать найти JSON в ```json блоке
    match = re.search(r"```json\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Попробовать парсить весь текст
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Попробовать найти первый { ... }
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


async def dream_loop(
    agent_dir: str,
    interval_hours: float = 2.0,
    model_phase1: str = "haiku",
    model_phase2: str = "sonnet",
    skill_advisor_config: dict | None = None,
    bus=None,
    agent_name: str = "",
) -> None:
    """
    Бесконечный цикл Dream-обработки.

    Запускается как asyncio.Task при старте агента.

    Args:
        skill_advisor_config: конфиг Phase 3 (анализ паттернов)
        bus: FleetBus для отправки предложений мастеру
        agent_name: имя агента
    """
    interval_seconds = interval_hours * 3600
    logger.info(
        f"Dream loop запущен для {agent_dir}, "
        f"интервал: {interval_hours}ч"
        + (", skill_advisor включён" if skill_advisor_config else "")
    )

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            result = await dream_cycle(
                agent_dir,
                model_phase1,
                model_phase2,
                skill_advisor_config=skill_advisor_config,
                bus=bus,
                agent_name=agent_name,
            )
            logger.info(f"Dream result: {result}")
        except asyncio.CancelledError:
            logger.info("Dream loop остановлен")
            break
        except Exception as e:
            logger.error(f"Dream loop error: {e}")
