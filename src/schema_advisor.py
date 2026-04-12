"""
SchemaAdvisor — фоновый модуль проактивных предложений по улучшению схемы
vault'а Архивариуса.

Работает в Dream-цикле на worker-агентах с типом архивариуса (включается флагом
`schema_advisor.enabled: true` в agent.yaml). Анализирует `wiki/log.md` и карточки
vault'а, ищет четыре класса паттернов:

1. Пробелы в схеме — пользователь спрашивал про поле, которого нет в шаблоне
2. Повторяющиеся ручные операции — одинаковые поиски, которые можно автоматизировать
3. Теги вне таксономии — сигнал, что пора расширить `.vault-config.json → tags`
4. Новые типы документов — документы, не подходящие под существующие `document_types`

Копит предложения в `memory/schema_suggestions/inbox/`. Владелец видит их
мягкими упоминаниями в разговоре и в еженедельном дайджесте. Автоматически
ничего не меняет — любое изменение схемы проходит через скилл `schema-evolve`
по явному согласию Owner'а.

Паттерн реализации зеркально повторяет src/skill_advisor.py.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import memory

logger = logging.getLogger(__name__)

SUGGESTIONS_DIR = "schema_suggestions"
INBOX_DIR = "schema_suggestions/inbox"
ARCHIVE_DIR = "schema_suggestions/archive"
DISMISSED_FILE = "schema_suggestions/dismissed.json"


# ── Встроенный промпт для анализа ──────────────────────────────────────────

_BUILTIN_PROMPT = """\
# Анализ схемы архива на возможные улучшения

Ты — аналитик, изучающий как Архивариус (AI-агент для документных архивов)
работает с конкретным vault'ом. Твоя задача — найти сигналы, что доменная
схема может быть улучшена, и предложить конкретные изменения.

## Данные для анализа

### Текущая схема
{schema}

### Текущая таксономия (.vault-config.json)
{config}

### Журнал действий за последние {days} дней (wiki/log.md)
{log_excerpt}

### Обзор карточек в vault'е
{vault_overview}

### Ранее отклонённые предложения (не повторяй их)
{dismissed}

## Что искать

1. **Пробелы в схеме** — записи в логе, где пользователь спросил что-то,
   чего нет в текущих полях карточек. Примеры: "спрашивал про X поле", "поле Y
   отсутствует в карточке". Если такое повторилось ≥3 раз — это сигнал.

2. **Повторяющиеся ручные операции** — одинаковые типы поиска/подсчёта,
   которые пользователь делает регулярно. Пример: каждую неделю спрашивает
   "какие договоры истекают". Это можно вынести в автоматический отчёт.

3. **Теги вне таксономии** — теги, появляющиеся в карточках, но отсутствующие
   в `.vault-config.json → tags`. Каждое такое появление — кандидат на
   расширение таксономии.

4. **Новые типы документов** — документы с `document_subtype: unknown` или
   классифицированные неопределённо. Если таких накопилось ≥3 — есть основание
   добавить новый подтип.

## Правила

- Предлагай только **конкретные и применимые** изменения, не общие пожелания
- Если паттернов не найдено — верни пустой массив, это нормально
- Не предлагай деструктивных изменений (удаление сущностей, переименование
  ключевых полей)
- Не предлагай того, что уже в списке `dismissed` — владелец уже отклонил это

## Формат ответа (строго JSON)

```json
{{
  "suggestions": [
    {{
      "id_hint": "короткая уникальная фраза для стабильной идентификации",
      "category": "schema_gap | repeated_operation | off_taxonomy_tag | new_document_type",
      "observation": "что именно я заметил, с количественными данными",
      "evidence": ["пример 1 из лога", "пример 2 из лога"],
      "frequency": 5,
      "proposal": "конкретное предложение — что именно изменить в схеме",
      "impact": "low | medium | high",
      "confidence": "low | medium | high"
    }}
  ],
  "summary": "краткая сводка по состоянию схемы"
}}
```

Предлагай только suggestions с confidence "high" или "medium" и frequency ≥ 3.
"""


# ── Публичная функция для вызова из Dream ──────────────────────────────────


async def analyze_vault(
    agent_dir: str,
    agent_name: str,
    model: str = "haiku",
    days: int = 7,
) -> list[dict]:
    """
    Проанализировать vault и найти предложения по улучшению схемы.

    Вызывается из Dream-цикла агентов, у которых
    `schema_advisor.enabled: true` в agent.yaml.

    Returns:
        Список предложений по схеме (уже отфильтрованных от dismissed).
    """
    from .dream import _call_claude_simple, _extract_json

    memory_path = memory.get_memory_path(agent_dir)

    # Проверка что vault инициализирован
    config_path = memory_path / ".vault-config.json"
    if not config_path.exists():
        logger.info(f"SchemaAdvisor [{agent_name}]: .vault-config.json нет, vault не инициализирован")
        return []

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"SchemaAdvisor [{agent_name}]: не удалось прочитать config: {e}")
        return []

    if not config.get("initialized"):
        logger.info(f"SchemaAdvisor [{agent_name}]: vault не инициализирован")
        return []

    # Собрать данные для анализа
    log_excerpt = _collect_log_excerpt(memory_path, days=days)
    if not log_excerpt:
        logger.info(f"SchemaAdvisor [{agent_name}]: журнал пуст, нечего анализировать")
        return []

    schema = _read_file_safe(memory_path / "SCHEMA.md")
    vault_overview = _build_vault_overview(memory_path)
    dismissed = _load_dismissed(agent_dir)

    # Сформировать промпт
    prompt = _BUILTIN_PROMPT.format(
        schema=schema or "(SCHEMA.md пуст)",
        config=json.dumps(config, ensure_ascii=False, indent=2),
        days=days,
        log_excerpt=log_excerpt,
        vault_overview=vault_overview or "(vault пуст)",
        dismissed=json.dumps(dismissed, ensure_ascii=False) if dismissed else "[]",
    )

    # Вызвать Claude
    try:
        response = await _call_claude_simple(
            prompt, model=model, cwd=str(memory_path)
        )
    except Exception as e:
        logger.error(f"SchemaAdvisor [{agent_name}] Claude error: {e}")
        return []

    # Парсить ответ
    data = _extract_json(response)
    if not data:
        logger.warning(f"SchemaAdvisor [{agent_name}]: не удалось извлечь JSON")
        return []

    raw_suggestions = data.get("suggestions", [])
    if not raw_suggestions:
        logger.info(f"SchemaAdvisor [{agent_name}]: предложений нет")
        return []

    # Фильтрация и обогащение метаданными
    now = datetime.now().isoformat()
    suggestions = []
    for s in raw_suggestions:
        if s.get("confidence") not in ("high", "medium"):
            continue
        if s.get("frequency", 0) < 3:
            continue
        # Защита от повторов из dismissed
        id_hint = s.get("id_hint", "")
        if id_hint and id_hint in dismissed:
            continue
        suggestion = {
            "id": uuid4().hex[:8],
            "id_hint": id_hint,
            "timestamp": now,
            "agent_name": agent_name,
            "category": s.get("category", "schema_gap"),
            "observation": s.get("observation", ""),
            "evidence": s.get("evidence", []),
            "frequency": s.get("frequency", 0),
            "proposal": s.get("proposal", ""),
            "impact": s.get("impact", "medium"),
            "confidence": s.get("confidence", "medium"),
            "status": "pending",
        }
        suggestions.append(suggestion)

    logger.info(
        f"SchemaAdvisor [{agent_name}]: найдено {len(suggestions)} предложений"
    )
    return suggestions


# ── Сохранение и управление предложениями ──────────────────────────────────


def store_suggestions(agent_dir: str, suggestions: list[dict]) -> None:
    """Сохранить предложения в memory/schema_suggestions/inbox/."""
    if not suggestions:
        return
    memory_path = memory.get_memory_path(agent_dir)
    inbox = memory_path / INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)

    for s in suggestions:
        filename = f"{s['timestamp'][:10]}_{s['id']}.json"
        path = inbox / filename
        try:
            path.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning(f"Не удалось сохранить {filename}: {e}")


def list_pending_suggestions(agent_dir: str) -> list[dict]:
    """Получить список всех pending-предложений из inbox."""
    memory_path = memory.get_memory_path(agent_dir)
    inbox = memory_path / INBOX_DIR
    if not inbox.exists():
        return []

    result = []
    for f in sorted(inbox.glob("*.json")):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
            if s.get("status") == "pending":
                result.append(s)
        except (json.JSONDecodeError, OSError):
            continue
    return result


def mark_suggestion(agent_dir: str, suggestion_id: str, status: str) -> bool:
    """
    Изменить статус предложения: 'accepted', 'dismissed', 'snoozed'.

    dismissed-предложения попадают в dismissed.json, чтобы не повторяться.
    """
    if status not in ("accepted", "dismissed", "snoozed"):
        logger.warning(f"Неизвестный статус: {status}")
        return False

    memory_path = memory.get_memory_path(agent_dir)
    inbox = memory_path / INBOX_DIR
    archive = memory_path / ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)

    for f in inbox.glob("*.json"):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if s.get("id") != suggestion_id:
            continue

        s["status"] = status
        s["resolved_at"] = datetime.now().isoformat()

        # Перенести в архив
        dest = archive / f.name
        try:
            dest.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            f.unlink()
        except OSError as e:
            logger.warning(f"Не удалось переместить {f.name} в архив: {e}")
            return False

        # Если dismissed — добавить id_hint в список для защиты от повторов
        if status == "dismissed":
            id_hint = s.get("id_hint")
            if id_hint:
                _add_dismissed(agent_dir, id_hint)

        return True

    return False


def build_digest(agent_dir: str) -> str:
    """
    Собрать еженедельный дайджест предложений для владельца.

    Возвращает markdown-строку для отправки в outbox.
    """
    pending = list_pending_suggestions(agent_dir)
    if not pending:
        return ""

    # Группируем по категориям
    by_category: dict[str, list[dict]] = {}
    for s in pending:
        cat = s.get("category", "schema_gap")
        by_category.setdefault(cat, []).append(s)

    category_titles = {
        "schema_gap": "Пробелы в схеме",
        "repeated_operation": "Повторяющиеся операции",
        "off_taxonomy_tag": "Теги вне таксономии",
        "new_document_type": "Новые типы документов",
    }

    lines = [
        "# Дайджест предложений SchemaAdvisor",
        "",
        f"За прошедшую неделю накопилось {len(pending)} предложений по улучшению схемы.",
        "Ты можешь принять, отклонить или отложить каждое.",
        "",
    ]

    for cat, items in by_category.items():
        lines.append(f"## {category_titles.get(cat, cat)}")
        lines.append("")
        for s in items:
            lines.append(f"### [{s['id']}] {s['observation']}")
            lines.append(f"**Частота:** {s['frequency']} упоминаний")
            lines.append(f"**Предложение:** {s['proposal']}")
            lines.append(f"**Влияние:** {s['impact']}")
            if s.get("evidence"):
                lines.append(f"**Примеры:** {', '.join(s['evidence'][:3])}")
            lines.append("")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "Чтобы применить предложение, скажи: «принять {id}», «отклонить {id}» "
        "или «отложить {id}»."
    )
    return "\n".join(lines)


# ── Вспомогательные функции ────────────────────────────────────────────────


def _collect_log_excerpt(memory_path: Path, days: int = 7, max_chars: int = 8000) -> str:
    """Прочитать релевантную часть wiki/log.md за последние N дней."""
    log_path = memory_path / "wiki" / "log.md"
    if not log_path.exists():
        return ""

    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    # Фильтруем строки по дате (если присутствует в формате YYYY-MM-DD)
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    relevant_lines = []
    for line in text.splitlines():
        # Ищем строки вида "- YYYY-MM-DD HH:MM — ..."
        if not line.strip().startswith("-"):
            continue
        # Извлекаем дату
        parts = line.split(maxsplit=2)
        if len(parts) < 2:
            continue
        date_candidate = parts[1]
        if len(date_candidate) >= 10 and date_candidate[:10] >= cutoff_str:
            relevant_lines.append(line)

    excerpt = "\n".join(relevant_lines)
    if len(excerpt) > max_chars:
        excerpt = excerpt[-max_chars:]
        # Обрезаем до начала строки
        nl = excerpt.find("\n")
        if nl > 0:
            excerpt = excerpt[nl + 1 :]
    return excerpt


def _build_vault_overview(memory_path: Path) -> str:
    """Сводка по содержимому vault'а: количество карточек по типам."""
    wiki = memory_path / "wiki"
    if not wiki.exists():
        return ""

    lines = []
    for sub in sorted(wiki.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name.startswith("_") or sub.name.startswith("."):
            continue
        md_files = list(sub.glob("*.md"))
        # Исключаем индекс
        md_files = [f for f in md_files if not f.name.startswith("_")]
        if md_files:
            lines.append(f"- {sub.name}/: {len(md_files)} карточек")
    return "\n".join(lines)


def _load_dismissed(agent_dir: str) -> list[str]:
    """Загрузить список ранее отклонённых id_hint."""
    memory_path = memory.get_memory_path(agent_dir)
    path = memory_path / DISMISSED_FILE
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _add_dismissed(agent_dir: str, id_hint: str) -> None:
    """Добавить id_hint в список отклонённых, чтобы не предлагать снова."""
    memory_path = memory.get_memory_path(agent_dir)
    path = memory_path / DISMISSED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    current = _load_dismissed(agent_dir)
    if id_hint in current:
        return
    current.append(id_hint)
    try:
        path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"Не удалось обновить dismissed.json: {e}")


def _read_file_safe(path: Path) -> str:
    """Безопасно прочитать файл."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
