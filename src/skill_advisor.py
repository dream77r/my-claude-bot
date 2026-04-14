"""
SkillAdvisor — анализ паттернов и предложение скиллов.

Изолированные агенты анализируют разговоры с пользователями,
выявляют часто повторяющиеся паттерны и предлагают новые скиллы.

Поток данных:
1. Dream Phase 3 → analyze_patterns() → выявление паттернов
2. store_suggestions() → сохранение в memory/skill_suggestions/
3. report_to_master() → отправка предложений оркестратору через bus
4. Master: receive_suggestion() → сохранение в inbox
5. Master: compile_daily_digest() → ежедневная сводка владельцу
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import memory
from .bus import FleetBus, FleetMessage, MessageType

logger = logging.getLogger(__name__)

SUGGESTIONS_DIR = "skill_suggestions"
INBOX_DIR = "skill_suggestions/inbox"
ARCHIVE_DIR = "skill_suggestions/archive"


# ── Phase 3: Анализ паттернов (запускается в Dream для worker-агентов) ──


def _load_analysis_prompt(agent_dir: str) -> str:
    """Загрузить промпт для анализа паттернов."""
    template_path = Path(agent_dir) / "templates" / "skill_advisor.md"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    # Fallback: глобальный шаблон
    global_path = Path(agent_dir).parent.parent / "templates" / "skill_advisor.md"
    if global_path.exists():
        return global_path.read_text(encoding="utf-8")

    # Встроенный шаблон
    return _BUILTIN_PROMPT


_BUILTIN_PROMPT = """\
# Анализ паттернов использования

Ты — аналитик, изучающий как пользователь общается с AI-агентом.
Твоя задача — найти повторяющиеся паттерны и предложить новые скиллы.

## Данные для анализа

### Профиль пользователя
{profile}

### История разговоров (последние дни)
{conversations}

### Текущие скиллы агента
{current_skills}

### Существующие wiki-знания
{wiki_index}

## Инструкции

1. Проанализируй разговоры и найди:
   - Повторяющиеся типы запросов (≥3 раз за период)
   - Частые рабочие процессы, которые можно автоматизировать
   - Темы, по которым пользователь регулярно просит помощь
   - Типовые задачи, где скилл бы ускорил работу

2. Для каждого найденного паттерна предложи скилл:
   - Учти, что скилл — это markdown-инструкция для Claude
   - Скилл должен решать конкретную проблему, не быть слишком общим
   - Не предлагай скиллы, которые дублируют существующие

3. Если паттернов не обнаружено — верни пустой массив

## Формат ответа (строго JSON)

```json
{
  "patterns": [
    {
      "pattern": "Описание выявленного паттерна",
      "frequency": 5,
      "examples": ["пример запроса 1", "пример запроса 2"],
      "suggested_skill": {
        "name": "skill-name-kebab-case",
        "title": "Название скилла",
        "description": "Что делает скилл и какую проблему решает",
        "capabilities": ["возможность 1", "возможность 2"]
      },
      "confidence": "high"
    }
  ],
  "summary": "Общая сводка по использованию агента"
}
```

confidence: "high" (≥5 раз), "medium" (3-4 раза), "low" (2 раза, но явный паттерн)
Предлагай только паттерны с confidence "high" или "medium".
"""


async def analyze_patterns(
    agent_dir: str,
    agent_name: str,
    model: str = "haiku",
    days: int = 7,
) -> list[dict]:
    """
    Анализировать разговоры и выявить паттерны.

    Returns:
        Список предложений по скиллам
    """
    from . import get_claude_cli_path
    from .dream import _call_claude_simple, _extract_json

    memory_path = memory.get_memory_path(agent_dir)

    # Собрать данные для анализа
    conversations = _collect_conversations(agent_dir, days=days)
    if not conversations:
        logger.info(f"SkillAdvisor [{agent_name}]: нет разговоров для анализа")
        return []

    # Минимум сообщений для анализа
    if len(conversations) < 10:
        logger.info(
            f"SkillAdvisor [{agent_name}]: слишком мало сообщений "
            f"({len(conversations)}), нужно ≥10"
        )
        return []

    profile = _read_file_safe(memory_path / "profile.md")
    wiki_index = _read_file_safe(memory_path / "index.md")
    current_skills = _get_current_skills(agent_dir)

    # Загрузить промпт
    prompt_template = _load_analysis_prompt(agent_dir)
    prompt = prompt_template.format(
        profile=profile or "(профиль не заполнен)",
        conversations=_format_conversations(conversations),
        current_skills=current_skills or "(скиллов нет)",
        wiki_index=wiki_index or "(wiki пуст)",
    )

    # Вызвать Claude
    try:
        response = await _call_claude_simple(
            prompt, model=model, cwd=str(memory_path)
        )
    except Exception as e:
        logger.error(f"SkillAdvisor [{agent_name}] Claude error: {e}")
        return []

    # Парсить ответ
    data = _extract_json(response)
    if not data:
        logger.warning(f"SkillAdvisor [{agent_name}]: не удалось извлечь JSON")
        return []

    patterns = data.get("patterns", [])
    if not patterns:
        logger.info(f"SkillAdvisor [{agent_name}]: паттернов не обнаружено")
        return []

    # Добавить метаданные
    now = datetime.now().isoformat()
    suggestions = []
    for p in patterns:
        if p.get("confidence") not in ("high", "medium"):
            continue
        suggestion = {
            "id": uuid4().hex[:8],
            "timestamp": now,
            "agent_name": agent_name,
            "pattern": p.get("pattern", ""),
            "frequency": p.get("frequency", 0),
            "examples": p.get("examples", []),
            "suggested_skill": p.get("suggested_skill", {}),
            "confidence": p.get("confidence", "medium"),
        }
        suggestions.append(suggestion)

    logger.info(
        f"SkillAdvisor [{agent_name}]: найдено {len(suggestions)} предложений"
    )
    return suggestions


def _collect_conversations(agent_dir: str, days: int = 7) -> list[dict]:
    """Собрать сообщения за последние N дней."""
    memory_path = memory.get_memory_path(agent_dir)
    conv_dir = memory_path / "raw" / "conversations"

    if not conv_dir.exists():
        return []

    from datetime import timedelta

    cutoff = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    messages = []
    for f in sorted(conv_dir.glob("conversations-*.jsonl")):
        # Фильтр по дате из имени файла
        try:
            date_str = f.stem.replace("conversations-", "")
            if date_str < cutoff_str:
                continue
        except ValueError:
            continue

        try:
            for line in f.read_text(encoding="utf-8").strip().split("\n"):
                if line.strip():
                    messages.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            continue

    return messages


def _format_conversations(messages: list[dict], max_chars: int = 8000) -> str:
    """Форматировать сообщения для промпта."""
    lines = []
    total = 0
    for m in messages:
        ts = m.get("timestamp", "?")[:16]
        role = m.get("role", "?")
        content = m.get("content", "")[:300]
        line = f"[{ts}] {role}: {content}"
        if total + len(line) > max_chars:
            lines.append("... (обрезано)")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _get_current_skills(agent_dir: str) -> str:
    """Получить список текущих скиллов агента."""
    skills_dir = Path(agent_dir) / "skills"
    if not skills_dir.exists():
        return ""

    skills = []
    for f in skills_dir.glob("*.md"):
        skills.append(f"- {f.stem}")
    for d in skills_dir.iterdir():
        if d.is_dir() and (d / "SKILL.md").exists():
            skills.append(f"- {d.name}")
    return "\n".join(sorted(skills))


def _read_file_safe(path: Path) -> str:
    """Безопасно прочитать файл."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ── Хранение предложений ──


def store_suggestions(
    agent_dir: str, suggestions: list[dict]
) -> Path:
    """
    Сохранить предложения в memory/skill_suggestions/pending.json.

    Дописывает к существующим, не перезаписывает.
    """
    memory_path = memory.get_memory_path(agent_dir)
    suggestions_dir = memory_path / SUGGESTIONS_DIR
    suggestions_dir.mkdir(parents=True, exist_ok=True)

    pending_file = suggestions_dir / "pending.json"

    existing = []
    if pending_file.exists():
        try:
            data = json.loads(pending_file.read_text(encoding="utf-8"))
            existing = data.get("suggestions", [])
        except (json.JSONDecodeError, OSError):
            pass

    # Дедупликация по skill name
    existing_names = {
        s.get("suggested_skill", {}).get("name") for s in existing
    }
    new = [
        s for s in suggestions
        if s.get("suggested_skill", {}).get("name") not in existing_names
    ]

    if not new:
        logger.info("SkillAdvisor: все предложения уже существуют, пропускаю")
        return pending_file

    all_suggestions = existing + new

    pending_file.write_text(
        json.dumps(
            {"suggestions": all_suggestions, "updated": datetime.now().isoformat()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    logger.info(f"SkillAdvisor: сохранено {len(new)} новых предложений")
    return pending_file


# ── Отправка мастеру (worker → master) ──


async def report_to_master(
    agent_name: str,
    suggestions: list[dict],
    bus: FleetBus,
    master_name: str = "me",
) -> bool:
    """
    Отправить предложения по скиллам мастер-агенту через bus.

    Формат сообщения: SYSTEM с metadata.type = "skill_suggestions"
    """
    if not suggestions:
        return False

    payload = json.dumps(
        {
            "type": "skill_suggestions",
            "agent_name": agent_name,
            "timestamp": datetime.now().isoformat(),
            "suggestions": suggestions,
        },
        ensure_ascii=False,
    )

    msg = FleetMessage(
        source=f"agent:{agent_name}",
        target=f"skill_inbox:{master_name}",
        content=payload,
        msg_type=MessageType.SYSTEM,
        metadata={
            "type": "skill_suggestions",
            "source_agent": agent_name,
            "count": len(suggestions),
        },
    )

    delivered = await bus.publish(msg)
    if delivered > 0:
        logger.info(
            f"SkillAdvisor [{agent_name}]: отправлено {len(suggestions)} "
            f"предложений мастеру '{master_name}'"
        )
        return True
    else:
        logger.warning(
            f"SkillAdvisor [{agent_name}]: не удалось доставить "
            f"предложения мастеру '{master_name}'"
        )
        return False


# ── Master-side: приём и хранение ──


def receive_suggestion(master_agent_dir: str, msg: FleetMessage) -> bool:
    """
    Принять предложения от worker-агента и сохранить в inbox.

    Вызывается из SkillAdvisorListener при получении SYSTEM сообщения
    с metadata.type == "skill_suggestions".
    """
    try:
        data = json.loads(msg.content)
    except (json.JSONDecodeError, TypeError):
        logger.error("SkillAdvisor: невалидный JSON в предложении")
        return False

    agent_name = data.get("agent_name", "unknown")
    suggestions = data.get("suggestions", [])

    if not suggestions:
        return False

    memory_path = memory.get_memory_path(master_agent_dir)
    inbox_dir = memory_path / INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)

    # Сохранить в inbox/{agent_name}_{date}.json
    date_str = datetime.now().strftime("%Y-%m-%d")
    inbox_file = inbox_dir / f"{agent_name}_{date_str}.json"

    existing = []
    if inbox_file.exists():
        try:
            existing = json.loads(inbox_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing.extend(suggestions)

    inbox_file.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info(
        f"SkillAdvisor: inbox мастера пополнен: {len(suggestions)} "
        f"предложений от '{agent_name}'"
    )
    return True


# ── Master-side: ежедневный дайджест ──


def compile_daily_digest(master_agent_dir: str) -> str | None:
    """
    Собрать ежедневный дайджест предложений из inbox.

    Читает все файлы из inbox/, компилирует сводку,
    перемещает обработанные в archive/.

    Returns:
        Текст дайджеста или None если предложений нет
    """
    memory_path = memory.get_memory_path(master_agent_dir)
    inbox_dir = memory_path / INBOX_DIR
    archive_dir = memory_path / ARCHIVE_DIR

    if not inbox_dir.exists():
        return None

    inbox_files = list(inbox_dir.glob("*.json"))
    if not inbox_files:
        return None

    # Собрать все предложения
    all_suggestions: dict[str, list[dict]] = {}  # agent_name → [suggestions]
    for f in inbox_files:
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                continue
            for item in items:
                agent = item.get("agent_name", "unknown")
                all_suggestions.setdefault(agent, []).append(item)
        except (json.JSONDecodeError, OSError):
            continue

    if not all_suggestions:
        return None

    # Сформировать дайджест
    lines = [
        "📊 **Дайджест предложений по скиллам**",
        f"Дата: {datetime.now().strftime('%Y-%m-%d')}",
        "",
    ]

    total = 0
    for agent_name, suggestions in all_suggestions.items():
        lines.append(f"━━━ Агент: **{agent_name}** ━━━")
        lines.append("")

        for s in suggestions:
            skill = s.get("suggested_skill", {})
            conf = s.get("confidence", "?")
            freq = s.get("frequency", "?")
            conf_emoji = "🟢" if conf == "high" else "🟡"

            lines.append(f"{conf_emoji} **{skill.get('title', '?')}** (`{skill.get('name', '?')}`)")
            lines.append(f"   Паттерн: {s.get('pattern', '?')}")
            lines.append(f"   Частота: {freq}x | Уверенность: {conf}")
            lines.append(f"   Описание: {skill.get('description', '?')}")

            examples = s.get("examples", [])
            if examples:
                lines.append(f"   Примеры: {'; '.join(examples[:3])}")

            capabilities = skill.get("capabilities", [])
            if capabilities:
                for cap in capabilities[:3]:
                    lines.append(f"   • {cap}")

            lines.append("")
            total += 1

    lines.append(f"Итого: **{total}** предложений от **{len(all_suggestions)}** агентов")
    lines.append("")
    lines.append(
        "💡 Ответь какие скиллы создать — я подготовлю их и добавлю агентам."
    )

    # Архивировать обработанные файлы
    archive_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    for f in inbox_files:
        archive_path = archive_dir / f"{date_str}_{f.name}"
        f.rename(archive_path)

    logger.info(f"SkillAdvisor digest: {total} предложений архивировано")

    return "\n".join(lines)


# ── Ежедневный дайджест (cron) ──


async def run_daily_digest(
    master_agent_dir: str,
    master_name: str,
    bus: FleetBus,
    chat_id: int = 0,
) -> None:
    """
    Запустить ежедневную проверку предложений и отправить дайджест.

    Вызывается из cron-задачи master-агента.
    """
    digest = compile_daily_digest(master_agent_dir)

    if not digest:
        logger.info("SkillAdvisor digest: нет новых предложений")
        return

    # Отправить дайджест владельцу через Telegram
    notification = FleetMessage(
        source=f"agent:{master_name}",
        target=f"telegram:{master_name}",
        content=digest,
        msg_type=MessageType.OUTBOUND,
        chat_id=chat_id,
    )
    await bus.publish(notification)

    logger.info("SkillAdvisor: дайджест отправлен владельцу")

    # Git commit
    memory.git_commit(
        master_agent_dir, "SkillAdvisor: daily digest sent"
    )


async def skill_digest_loop(
    master_agent_dir: str,
    master_name: str,
    bus: FleetBus,
    check_hour: int = 20,
    chat_id: int = 0,
) -> None:
    """
    Ежедневный цикл проверки и отправки дайджеста.

    Проверяет inbox раз в час, отправляет дайджест
    в заданный час (по умолчанию 20:00).

    Args:
        check_hour: час отправки дайджеста (0-23)
        chat_id: chat_id владельца для уведомлений
    """
    logger.info(
        f"SkillDigest loop запущен для '{master_name}', "
        f"дайджест в {check_hour}:00"
    )

    last_sent_date = ""

    while True:
        try:
            await asyncio.sleep(3600)  # Проверять раз в час

            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            # Отправлять только раз в день в заданный час
            if now.hour == check_hour and today != last_sent_date:
                await run_daily_digest(
                    master_agent_dir, master_name, bus, chat_id
                )
                last_sent_date = today

        except asyncio.CancelledError:
            logger.info(f"SkillDigest loop '{master_name}' остановлен")
            break
        except Exception as e:
            logger.error(f"SkillDigest loop error: {e}")


# ── Receiver для master-агента ──


import asyncio


class SkillSuggestionReceiver:
    """
    Принимает SYSTEM-сообщения с предложениями скиллов для master-агента.

    Подписывается на специальную очередь в bus и обрабатывает
    входящие предложения от worker-агентов.
    """

    def __init__(self, master_agent_dir: str, master_name: str, bus: FleetBus):
        self.master_agent_dir = master_agent_dir
        self.master_name = master_name
        self.bus = bus
        self.queue_name = f"skill_inbox:{master_name}"

    async def run(self) -> None:
        """Слушать bus на предложения по скиллам."""
        self.bus.subscribe(self.queue_name)
        logger.info(
            f"SkillSuggestionReceiver запущен для '{self.master_name}'"
        )

        while True:
            try:
                msg = await self.bus.consume(self.queue_name)

                # Проверяем что это предложение по скиллам
                if msg.metadata.get("type") == "skill_suggestions":
                    receive_suggestion(self.master_agent_dir, msg)
                    logger.info(
                        f"Получено предложение от '{msg.source}': "
                        f"{msg.metadata.get('count', '?')} скиллов"
                    )
            except asyncio.CancelledError:
                logger.info(
                    f"SkillSuggestionReceiver '{self.master_name}' остановлен"
                )
                break
            except Exception as e:
                logger.error(f"SkillSuggestionReceiver error: {e}")
                await asyncio.sleep(5)
