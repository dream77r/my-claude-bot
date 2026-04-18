"""
Knowledge Graph — 3-уровневый граф связей памяти.

Ночной пайплайн (01:00 UTC / 04:00 MSK):
  Уровень 1: Линковка — [[Obsidian]] связи внутри дневного лога
  Уровень 2: Саммари — итоги дня с [[ссылками]] на wiki
  Уровень 3: Синтез — перелинковка всех саммари + обновление графа

Уровень 3 имеет адаптивную частоту:
  - Первые N дней: каждый день (обучение)
  - Потом: каждые M дней (поддержание)
  Настраивается через agent.yaml: knowledge_graph.synthesis_schedule

Все уровни используют haiku (дешёвую модель).
На Pro подписке — ноль дополнительных расходов.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import get_claude_cli_path, memory

logger = logging.getLogger(__name__)

# Файл для хранения графа связей
GRAPH_FILE = "graph.json"
# Директория для daily summaries
SUMMARIES_DIR = "daily/summaries"
# Файл для трекинга состояния синтеза
SYNTHESIS_STATE_FILE = "sessions/.synthesis_state.json"

# 9 типов entity (Этап 3 KG_WIKI_PLAN.md). Каждый тип кладётся в свою папку
# wiki/<folder>/. Имя типа в JSON — каноническое (CamelCase), но мы аккуратно
# принимаем и lowercase.
_ENTITY_TYPE_TO_FOLDER: dict[str, str] = {
    "Person": "people",
    "Company": "companies",
    "Project": "projects",
    "Decision": "decisions",
    "Idea": "ideas",
    "Event": "events",
    "Topic": "topics",
    "Claim": "claims",
    "Document": "documents",
}


def _normalize_entity_type(raw: str | None) -> str:
    """Привести тип к каноническому виду. Неизвестное → 'Topic'."""
    if not raw:
        return "Topic"
    raw_l = raw.strip().lower()
    for canonical in _ENTITY_TYPE_TO_FOLDER:
        if canonical.lower() == raw_l:
            return canonical
    # Обратная совместимость со старой 6-типовой схемой
    legacy_map = {
        "person": "Person",
        "people": "Person",
        "company": "Company",
        "organization": "Company",
        "project": "Project",
        "product": "Project",
        "concept": "Idea",
        "tool": "Document",
        "event": "Event",
        "decision": "Decision",
        "idea": "Idea",
        "topic": "Topic",
        "claim": "Claim",
        "document": "Document",
    }
    return legacy_map.get(raw_l, "Topic")


def _entity_folder(entity_type: str) -> str:
    return _ENTITY_TYPE_TO_FOLDER.get(_normalize_entity_type(entity_type), "topics")


# Supersession (Этап 4 KG_WIKI_PLAN.md). Для типов связей из этого набора
# работает правило "single-value": одна сущность может иметь только одну
# *активную* связь такого типа. Когда LLM извлекает новую — старая
# помечается superseded_by, но не удаляется (граф = история, не state).
_EXCLUSIVE_LINK_TYPES = frozenset({
    "works_at",   # один основной работодатель
    "owns",       # один владелец
    "lives_in",   # одно место жительства
    "leads",      # один лидер проекта
})


def _edge_id(edge: dict) -> str:
    """Стабильный id ребра — для ссылок supersedes / superseded_by."""
    parts = [
        edge.get("from", ""),
        edge.get("to", ""),
        edge.get("type", ""),
        edge.get("first_seen", "") or edge.get("date", ""),
    ]
    return "|".join(parts)


def _apply_supersession(graph: dict, new_edge: dict) -> None:
    """
    Применить supersession-логику для нового ребра.

    1. Если тип ребра — exclusive, и в графе уже есть активное (не superseded)
       ребро с тем же `from` и `type`, но другим `to` — старое помечается
       superseded_by нового.
    2. Если LLM явно прислал `supersedes: <name>` — пометить указанные.
    """
    new_id = _edge_id(new_edge)
    new_type = new_edge.get("type", "")
    new_from = new_edge.get("from", "")
    new_to = new_edge.get("to", "")

    edges = graph.get("edges", [])

    # 1. Автоматический supersession для exclusive типов
    if new_type in _EXCLUSIVE_LINK_TYPES:
        for old in edges:
            if old.get("superseded_by"):
                continue
            if (
                old.get("type") == new_type
                and old.get("from") == new_from
                and old.get("to") != new_to
            ):
                old["superseded_by"] = new_id
                old["superseded_at"] = new_edge.get("date") or new_edge.get(
                    "last_seen", ""
                )

    # 2. Явный supersedes от LLM (имя сущности, которую новое утверждение отменяет)
    explicit = new_edge.get("supersedes")
    if explicit:
        targets = explicit if isinstance(explicit, list) else [explicit]
        target_set = {t.lower() for t in targets if isinstance(t, str)}
        for old in edges:
            if old.get("superseded_by"):
                continue
            if (
                old.get("from", "").lower() in target_set
                or old.get("to", "").lower() in target_set
            ):
                old["superseded_by"] = new_id
                old["superseded_at"] = new_edge.get("date", "")


def _safe_filename(name: str) -> str:
    """Превратить имя entity в безопасное имя файла."""
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "unnamed"


# Legacy-папки до типизации — туда ещё может быть положена entity из прошлых
# версий KG. Cross-folder lookup должен их тоже учитывать, чтобы не плодить
# дубликаты.
_ENTITY_LOOKUP_FOLDERS: tuple[str, ...] = tuple(_ENTITY_TYPE_TO_FOLDER.values()) + (
    "entities", "concepts",
)


def _find_existing_entity_page(memory_path: Path, safe_name: str) -> Path | None:
    """Найти страницу с данным stem в любой типизированной или legacy-папке."""
    wiki = memory_path / "wiki"
    if not wiki.exists():
        return None
    for folder in _ENTITY_LOOKUP_FOLDERS:
        candidate = wiki / folder / f"{safe_name}.md"
        if candidate.exists():
            return candidate
    return None


def _touch_entity_page(page_path: Path, date_str: str) -> None:
    """Обновить last_seen и добавить дату в Упоминания у существующей страницы."""
    try:
        existing = page_path.read_text(encoding="utf-8")
    except OSError:
        return

    updated = re.sub(
        r"(?m)^last_seen:\s*\S+",
        f"last_seen: {date_str}",
        existing,
        count=1,
    )
    if f"- {date_str}" not in updated:
        if "## Упоминания" in updated:
            updated = updated.replace(
                "## Упоминания\n",
                f"## Упоминания\n\n- {date_str}\n",
                1,
            ).replace(
                f"## Упоминания\n\n- {date_str}\n\n- ",
                f"## Упоминания\n\n- {date_str}\n- ",
            )
        else:
            updated = updated.rstrip() + f"\n\n## Упоминания\n\n- {date_str}\n"

    if updated != existing:
        page_path.write_text(updated, encoding="utf-8")


def _ensure_entity_page(
    memory_path: Path,
    name: str,
    entity_type: str,
    date_str: str,
    confidence: float = 1.0,
) -> Path:
    """
    Создать (или обновить last_seen в) stub-страницу entity.

    Cross-folder lookup: если страница с таким именем уже лежит в любой
    wiki-папке (people/, projects/, legacy entities/ и т.п.), она
    обновляется in-place вне зависимости от переданного `entity_type`. Это
    защищает от дубликатов, когда L3 дописывает рёбра к entity, типизацию
    которой раньше сделал L1 в другой папке.

    Если страницы нет нигде — создаём новую в папке, соответствующей
    `entity_type` (по умолчанию topics/ для Topic).
    """
    canonical_type = _normalize_entity_type(entity_type)
    safe_name = _safe_filename(name)

    existing_page = _find_existing_entity_page(memory_path, safe_name)
    if existing_page is not None:
        _touch_entity_page(existing_page, date_str)
        return existing_page

    folder = _entity_folder(canonical_type)
    page_dir = memory_path / "wiki" / folder
    page_dir.mkdir(parents=True, exist_ok=True)
    page_path = page_dir / f"{safe_name}.md"

    content = (
        f"---\n"
        f"type: {canonical_type}\n"
        f"created: {date_str}\n"
        f"last_seen: {date_str}\n"
        f"confidence: {confidence}\n"
        f"---\n\n"
        f"# {name}\n\n"
        f"## Упоминания\n\n"
        f"- {date_str}\n"
    )
    page_path.write_text(content, encoding="utf-8")
    return page_path


# ── Утилиты ──


def _load_graph(agent_dir: str) -> dict:
    """Загрузить граф связей из graph.json."""
    memory_path = memory.get_memory_path(agent_dir)
    graph_path = memory_path / GRAPH_FILE
    if graph_path.exists():
        try:
            return json.loads(graph_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"edges": [], "updated": ""}


def _save_graph(agent_dir: str, graph: dict) -> None:
    """Сохранить граф связей."""
    memory_path = memory.get_memory_path(agent_dir)
    graph_path = memory_path / GRAPH_FILE
    graph["updated"] = datetime.now().isoformat()
    graph_path.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_synthesis_state(agent_dir: str) -> dict:
    """Загрузить состояние синтеза (когда последний раз, счётчик дней)."""
    memory_path = memory.get_memory_path(agent_dir)
    state_path = memory_path / SYNTHESIS_STATE_FILE
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_synthesis": None, "total_runs": 0, "first_run": None}


def _save_synthesis_state(agent_dir: str, state: dict) -> None:
    """Сохранить состояние синтеза."""
    memory_path = memory.get_memory_path(agent_dir)
    state_path = memory_path / SYNTHESIS_STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def should_run_synthesis(agent_dir: str, config: dict) -> bool:
    """
    Определить, нужно ли запускать Уровень 3 (синтез) сегодня.

    Адаптивная частота:
      - daily_phase_days: первые N дней запускать каждый день (по умолчанию 14)
      - regular_interval_days: потом каждые M дней (по умолчанию 3)

    Конфиг в agent.yaml:
      knowledge_graph:
        synthesis_schedule:
          daily_phase_days: 14
          regular_interval_days: 3
    """
    state = _load_synthesis_state(agent_dir)
    schedule = config.get("synthesis_schedule", {})
    daily_phase_days = schedule.get("daily_phase_days", 14)
    regular_interval = schedule.get("regular_interval_days", 3)

    today = datetime.now().strftime("%Y-%m-%d")

    # Первый запуск — всегда да
    if state["first_run"] is None:
        return True

    # Сколько дней с первого запуска
    try:
        first_date = datetime.strptime(state["first_run"], "%Y-%m-%d")
    except ValueError:
        return True
    days_since_first = (datetime.now() - first_date).days

    # Фаза обучения — каждый день
    if days_since_first < daily_phase_days:
        # Проверить что сегодня ещё не запускали
        return state.get("last_synthesis", "") != today

    # Фаза поддержания — каждые N дней
    last = state.get("last_synthesis", "")
    if not last:
        return True
    try:
        last_date = datetime.strptime(last, "%Y-%m-%d")
    except ValueError:
        return True
    days_since_last = (datetime.now() - last_date).days
    return days_since_last >= regular_interval


async def _call_claude_agent(
    prompt: str,
    model: str = "haiku",
    cwd: str | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """LLM-вызов Claude. С tools — агентный режим, без — простой."""
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


async def _call_claude_simple(
    prompt: str,
    model: str = "haiku",
    cwd: str | None = None,
) -> str:
    """Простой LLM-вызов (без tools). Shim вокруг _call_claude_agent."""
    return await _call_claude_agent(prompt, model=model, cwd=cwd)


def _load_template(agent_dir: str, template_name: str) -> str:
    """Загрузить промпт-шаблон из templates/."""
    path = Path(agent_dir) / "templates" / template_name
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning(f"Шаблон {template_name} не найден")
    return ""


# ── Source-фильтрация дневного лога ──
#
# KG-экстрактор должен видеть только пользовательский диалог, а не работу
# самой инфраструктуры агента. Иначе LLM извлекает имена внутренних компонентов
# (SmartTrigger, deadline_check, wiki) как entity и строит вокруг них фальшивые
# кластеры. См. KG_WIKI_PLAN.md, этап 1.

_BLOCK_USER_RE = re.compile(r"^\*\*\d{1,2}:\d{2}\*\*\s+👤")
_BLOCK_AGENT_RE = re.compile(r"^\*\*\d{1,2}:\d{2}\*\*\s+🤖")
_BLOCK_TRIGGER_RE = re.compile(r"^###\s*\[\d{1,2}:\d{2}\]\s*SmartTrigger:")
_BLOCK_DAY_HEADER_RE = re.compile(r"^#\s+\d{4}-\d{2}-\d{2}")
_AGENT_LINE_CONTENT_RE = re.compile(r"^\*\*\d{1,2}:\d{2}\*\*\s+🤖\s*(.*)$")


def _extract_user_content(daily_text: str) -> str:
    """
    Оставить в дневном логе только настоящий пользовательский диалог.

    Удаляются:
    - блоки SmartTrigger (`### [HH:MM] SmartTrigger: ...` до следующего блока);
    - автогенерированные секции `## Связи дня` и вложенные `### Упомянутые сущности`;
    - фоновые ответы агента (🤖) без предшествующего пользовательского сообщения;
    - пустые "обёртки" 🤖, которые на самом деле являются заголовками SmartTrigger.

    Сохраняются:
    - заголовок дня `# YYYY-MM-DD ...`;
    - пользовательские сообщения (👤);
    - прямые ответы агента (🤖), идущие сразу после 👤.
    """
    if not daily_text:
        return ""

    # 1. Вырезать `## Связи дня ...` целиком (до следующего `## ` или EOF).
    cleaned = re.sub(
        r"\n## Связи дня[^\n]*\n.*?(?=\n## |\Z)",
        "",
        daily_text,
        flags=re.DOTALL,
    )

    lines = cleaned.split("\n")

    # 2. Разбить на блоки. Каждый блок — это однотипная секция (user / agent /
    #    trigger / header), границы — по строкам, начинающим новый блок.
    BLOCK_HEADER = "header"
    BLOCK_USER = "user"
    BLOCK_AGENT = "agent"
    BLOCK_TRIGGER = "trigger"

    blocks: list[tuple[str, list[str]]] = []
    current_type: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_type is not None:
            blocks.append((current_type, list(current_lines)))

    for line in lines:
        if _BLOCK_DAY_HEADER_RE.match(line):
            _flush()
            current_type = BLOCK_HEADER
            current_lines = [line]
        elif _BLOCK_USER_RE.match(line):
            _flush()
            current_type = BLOCK_USER
            current_lines = [line]
        elif _BLOCK_AGENT_RE.match(line):
            _flush()
            current_type = BLOCK_AGENT
            current_lines = [line]
        elif _BLOCK_TRIGGER_RE.match(line):
            _flush()
            current_type = BLOCK_TRIGGER
            current_lines = [line]
        else:
            if current_type is None:
                current_type = BLOCK_HEADER
                current_lines = [line]
            else:
                current_lines.append(line)
    _flush()

    # 3. Отфильтровать блоки.
    result_chunks: list[str] = []
    user_pending = False  # был ли user-блок без ответа

    for btype, blines in blocks:
        if btype == BLOCK_HEADER:
            result_chunks.append("\n".join(blines))
            continue
        if btype == BLOCK_USER:
            result_chunks.append("\n".join(blines))
            user_pending = True
            continue
        if btype == BLOCK_AGENT:
            content_match = _AGENT_LINE_CONTENT_RE.match(blines[0])
            first_line_payload = content_match.group(1).strip() if content_match else ""
            rest_payload = "\n".join(blines[1:]).strip()
            has_content = bool(first_line_payload or rest_payload)
            if has_content and user_pending:
                result_chunks.append("\n".join(blines))
                user_pending = False
            # Иначе блок отбрасывается. user_pending не сбрасываем, если агент
            # был пустой — пользователь технически всё ещё ждёт ответа.
            continue
        if btype == BLOCK_TRIGGER:
            # Шум — отбрасываем, состояние не трогаем.
            continue

    return "\n".join(result_chunks).strip()


def _extract_json(text: str) -> dict | None:
    """Извлечь JSON из текста (может быть в ```json блоке)."""
    match = re.search(r"```json\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ── Уровень 1: Линковка ──


async def link_daily_entities(
    agent_dir: str, model: str = "haiku", date: datetime | None = None
) -> dict:
    """
    Уровень 1: Найти сущности и создать [[Obsidian]] связи в daily note.

    1. Читает дневной лог (daily/YYYY-MM-DD.md)
    2. LLM извлекает сущности и связи
    3. Добавляет секцию "Связи дня" в daily note
    4. Обновляет graph.json

    Returns:
        {"links_found": int, "entities": list, "ok": bool}
    """
    if date is None:
        date = datetime.now()

    result = {"links_found": 0, "entities": [], "ok": False}

    memory_path = memory.get_memory_path(agent_dir)
    date_str = date.strftime("%Y-%m-%d")
    daily_path = memory_path / "daily" / f"{date_str}.md"

    if not daily_path.exists():
        logger.info(f"KG Level 1: daily note {date_str} не найден")
        return result

    daily_content = daily_path.read_text(encoding="utf-8")
    if len(daily_content.strip()) < 50:
        logger.info(f"KG Level 1: daily note {date_str} слишком короткий")
        return result

    # Фильтр: только настоящий пользовательский диалог, без SmartTrigger-шума
    daily_content = _extract_user_content(daily_content)
    if len(daily_content.strip()) < 50:
        logger.info(
            f"KG Level 1: дневной user-контент {date_str} пуст после фильтрации"
        )
        return result

    # Прочитать существующие wiki-страницы для контекста
    wiki_dir = memory_path / "wiki"
    existing_pages = []
    if wiki_dir.exists():
        for md_file in wiki_dir.rglob("*.md"):
            rel = md_file.relative_to(memory_path)
            existing_pages.append(str(rel))

    # Загрузить шаблон
    template = _load_template(agent_dir, "kg_level1_links.md")
    if not template:
        # Fallback-промпт
        template = _LEVEL1_FALLBACK

    prompt = template.format(
        daily_content=daily_content,
        date=date_str,
        existing_pages="\n".join(f"- {p}" for p in existing_pages) if existing_pages else "(пусто)",
    )

    try:
        response = await _call_claude_simple(
            prompt, model=model, cwd=str(memory_path)
        )
    except Exception as e:
        logger.error(f"KG Level 1 error: {e}")
        return result

    # Парсим JSON
    data = _extract_json(response)
    if not data:
        logger.warning("KG Level 1: не удалось извлечь JSON")
        return result

    entities = data.get("entities", [])
    links = data.get("links", [])
    result["entities"] = entities
    result["links_found"] = len(links)

    # Создать/обновить stub-страницы для каждого entity (Этап 3)
    for entity in entities:
        ent_name = entity.get("name", "").strip()
        if not ent_name:
            continue
        # Принимаем оба ключа — новый "type" и старый "category"
        ent_type = entity.get("type") or entity.get("category") or "Topic"
        confidence = float(entity.get("confidence", 1.0) or 1.0)
        try:
            _ensure_entity_page(
                memory_path, ent_name, ent_type, date_str, confidence
            )
        except OSError as e:
            logger.warning(f"KG L1: не удалось создать страницу {ent_name}: {e}")

    # Добавить секцию "Связи дня" в daily note
    if links:
        links_section = f"\n\n## Связи дня ({date_str})\n\n"
        for link in links:
            from_entity = link.get("from", "")
            to_entity = link.get("to", "")
            context = link.get("context", "")
            links_section += f"- [[{from_entity}]] ↔ [[{to_entity}]] — {context}\n"

        if entities:
            links_section += "\n### Упомянутые сущности\n"
            for entity in entities:
                name = entity.get("name", "")
                category = entity.get("category", "entity")
                links_section += f"- [[{name}]] ({category})\n"

        # Проверить что секция ещё не добавлена
        if "## Связи дня" not in daily_content:
            with open(daily_path, "a", encoding="utf-8") as f:
                f.write(links_section)

        # Обновить graph.json
        graph = _load_graph(agent_dir)
        for link in links:
            edge = {
                "from": link.get("from", ""),
                "to": link.get("to", ""),
                "type": link.get("type", "related"),
                "context": link.get("context", ""),
                "date": date_str,
                "confidence": float(link.get("confidence", 1.0) or 1.0),
            }
            if link.get("supersedes"):
                edge["supersedes"] = link["supersedes"]

            # Проверить дубликат (та же пара + тот же день)
            is_dup = any(
                e.get("from") == edge["from"]
                and e.get("to") == edge["to"]
                and e.get("date") == edge["date"]
                for e in graph["edges"]
            )
            if not is_dup:
                # Обновить strength для существующих активных рёбер той же пары
                updated = False
                for existing in graph["edges"]:
                    if existing.get("superseded_by"):
                        continue
                    if (
                        existing.get("from") == edge["from"]
                        and existing.get("to") == edge["to"]
                        and existing.get("type") == edge["type"]
                    ):
                        existing["strength"] = existing.get("strength", 1) + 1
                        existing["last_seen"] = date_str
                        existing["context"] = edge["context"]
                        existing["confidence"] = max(
                            existing.get("confidence", 1.0), edge["confidence"]
                        )
                        updated = True
                        break
                if not updated:
                    edge["first_seen"] = date_str
                    edge["last_seen"] = date_str
                    edge["strength"] = 1
                    edge["id"] = _edge_id(edge)
                    _apply_supersession(graph, edge)
                    graph["edges"].append(edge)

        _save_graph(agent_dir, graph)

    result["ok"] = True
    logger.info(
        f"KG Level 1: {len(entities)} сущностей, "
        f"{len(links)} связей в {date_str}"
    )
    return result


# ── Уровень 2: Саммари ──


async def summarize_day(
    agent_dir: str, model: str = "haiku", date: datetime | None = None
) -> dict:
    """
    Уровень 2: Создать саммари дня с [[ссылками]].

    1. Читает daily note (с [[связями]] от Уровня 1)
    2. LLM создаёт итоги дня: темы, решения, действия
    3. Сохраняет в daily/summaries/YYYY-MM-DD.md

    Returns:
        {"topics": list, "decisions": list, "ok": bool}
    """
    if date is None:
        date = datetime.now()

    result = {"topics": [], "decisions": [], "ok": False}

    memory_path = memory.get_memory_path(agent_dir)
    date_str = date.strftime("%Y-%m-%d")
    daily_path = memory_path / "daily" / f"{date_str}.md"

    if not daily_path.exists():
        logger.info(f"KG Level 2: daily note {date_str} не найден")
        return result

    daily_content = daily_path.read_text(encoding="utf-8")
    if len(daily_content.strip()) < 50:
        logger.info(f"KG Level 2: daily note {date_str} слишком короткий")
        return result

    daily_content = _extract_user_content(daily_content)
    if len(daily_content.strip()) < 50:
        logger.info(
            f"KG Level 2: дневной user-контент {date_str} пуст после фильтрации"
        )
        return result

    # Загрузить шаблон
    template = _load_template(agent_dir, "kg_level2_summary.md")
    if not template:
        template = _LEVEL2_FALLBACK

    prompt = template.format(
        daily_content=daily_content,
        date=date_str,
    )

    try:
        response = await _call_claude_simple(
            prompt, model=model, cwd=str(memory_path)
        )
    except Exception as e:
        logger.error(f"KG Level 2 error: {e}")
        return result

    data = _extract_json(response)
    if not data:
        logger.warning("KG Level 2: не удалось извлечь JSON")
        return result

    topics = data.get("topics", [])
    decisions = data.get("decisions", [])
    summary_text = data.get("summary", "")
    action_items = data.get("action_items", [])

    result["topics"] = topics
    result["decisions"] = decisions

    # Сохранить саммари
    summaries_dir = memory_path / SUMMARIES_DIR
    summaries_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summaries_dir / f"{date_str}.md"

    weekday = date.strftime("%A")
    md_content = f"# Итоги дня: {date_str} ({weekday})\n\n"

    if summary_text:
        md_content += f"{summary_text}\n\n"

    if topics:
        md_content += "## Темы дня\n\n"
        for topic in topics:
            name = topic if isinstance(topic, str) else topic.get("name", "")
            md_content += f"- [[{name}]]\n"
        md_content += "\n"

    if decisions:
        md_content += "## Решения\n\n"
        for decision in decisions:
            if isinstance(decision, str):
                md_content += f"- {decision}\n"
            else:
                desc = decision.get("description", "")
                related = decision.get("related", [])
                links_str = ", ".join(f"[[{r}]]" for r in related)
                md_content += f"- {desc}"
                if links_str:
                    md_content += f" ({links_str})"
                md_content += "\n"
        md_content += "\n"

    if action_items:
        md_content += "## На завтра\n\n"
        for item in action_items:
            md_content += f"- [ ] {item}\n"
        md_content += "\n"

    summary_path.write_text(md_content, encoding="utf-8")

    result["ok"] = True
    logger.info(
        f"KG Level 2: {len(topics)} тем, "
        f"{len(decisions)} решений в {date_str}"
    )
    return result


# ── Уровень 3: Синтез графа ──


async def synthesize_graph(
    agent_dir: str,
    model: str = "haiku",
    max_summaries: int = 30,
) -> dict:
    """
    Уровень 3: Синтез связей между всеми саммари.

    1. Читает все daily summaries (последние max_summaries)
    2. Читает текущие synthesis-страницы
    3. LLM находит повторяющиеся темы, эволюцию решений, кластеры
    4. Обновляет wiki/synthesis/ и backlinks в wiki-страницах
    5. Обновляет graph.json cross-day связями

    Returns:
        {"patterns": list, "cross_links": int, "ok": bool}
    """
    result = {"patterns": [], "cross_links": 0, "ok": False}

    memory_path = memory.get_memory_path(agent_dir)
    summaries_dir = memory_path / SUMMARIES_DIR

    if not summaries_dir.exists():
        logger.info("KG Level 3: нет саммари для синтеза")
        return result

    # Собрать все саммари (последние max_summaries)
    summary_files = sorted(summaries_dir.glob("*.md"), reverse=True)
    summary_files = summary_files[:max_summaries]

    if len(summary_files) < 2:
        logger.info("KG Level 3: меньше 2 саммари, пропускаю")
        return result

    summaries_text = ""
    for sf in reversed(summary_files):  # хронологический порядок
        content = sf.read_text(encoding="utf-8")
        summaries_text += f"\n---\n{content}\n"

    # Текущие synthesis-страницы
    synthesis_dir = memory_path / "wiki" / "synthesis"
    existing_synthesis = ""
    if synthesis_dir.exists():
        for md_file in synthesis_dir.glob("*.md"):
            try:
                existing_synthesis += f"\n### {md_file.stem}\n"
                existing_synthesis += md_file.read_text(encoding="utf-8")[:500]
            except OSError:
                continue

    # Текущий граф
    graph = _load_graph(agent_dir)
    graph_summary = ""
    if graph["edges"]:
        # Top-10 сильнейших связей
        sorted_edges = sorted(
            graph["edges"], key=lambda e: e.get("strength", 0), reverse=True
        )
        for edge in sorted_edges[:10]:
            graph_summary += (
                f"- {edge['from']} ↔ {edge['to']} "
                f"(strength={edge.get('strength', 1)}, "
                f"since={edge.get('first_seen', '?')})\n"
            )

    # Загрузить шаблон
    template = _load_template(agent_dir, "kg_level3_synthesis.md")
    if not template:
        template = _LEVEL3_FALLBACK

    prompt = template.format(
        summaries=summaries_text,
        existing_synthesis=existing_synthesis or "(пусто)",
        graph_summary=graph_summary or "(пусто)",
        summary_count=len(summary_files),
    )

    try:
        response = await _call_claude_agent(
            prompt,
            model=model,
            cwd=str(memory_path),
            allowed_tools=["Read", "Write", "Edit", "Glob"],
        )
    except Exception as e:
        logger.error(f"KG Level 3 error: {e}")
        return result

    # Парсить JSON из ответа (если есть)
    data = _extract_json(response)
    if data:
        patterns = data.get("patterns", [])
        cross_links = data.get("cross_links", [])
        result["patterns"] = patterns
        result["cross_links"] = len(cross_links)

        # Добавить cross-day связи в граф
        today_str = datetime.now().strftime("%Y-%m-%d")
        for link in cross_links:
            edge = {
                "from": link.get("from", ""),
                "to": link.get("to", ""),
                "type": link.get("type", "cross_day"),
                "context": link.get("context", ""),
                "date": today_str,
                "first_seen": link.get("first_seen", "") or today_str,
                "last_seen": today_str,
                "strength": link.get("strength", 1),
                "confidence": float(link.get("confidence", 0.8) or 0.8),
            }
            # Проверить дубликат (симметрично + по типу)
            is_dup = any(
                (e.get("from") == edge["from"] and e.get("to") == edge["to"])
                or (e.get("from") == edge["to"] and e.get("to") == edge["from"])
                for e in graph["edges"]
                if e.get("type") == edge["type"]
            )
            if not is_dup:
                edge["id"] = _edge_id(edge)
                # Создать/обновить stub-страницы для обоих endpoint'ов,
                # чтобы lint не ругался на dangling edges. Тип по умолчанию
                # Topic: L3 не знает точного типа; если страница уже есть
                # в другой папке, cross-folder lookup в _ensure_entity_page
                # просто обновит её in-place.
                for endpoint in (edge["from"], edge["to"]):
                    if not endpoint:
                        continue
                    try:
                        _ensure_entity_page(
                            memory_path, endpoint, "Topic", today_str,
                        )
                    except OSError as e:
                        logger.warning(
                            f"KG L3: не удалось создать stub для '{endpoint}': {e}"
                        )
                graph["edges"].append(edge)

        _save_graph(agent_dir, graph)

    # Обновить состояние синтеза
    state = _load_synthesis_state(agent_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    state["last_synthesis"] = today
    state["total_runs"] = state.get("total_runs", 0) + 1
    if state["first_run"] is None:
        state["first_run"] = today
    _save_synthesis_state(agent_dir, state)

    result["ok"] = True
    logger.info(
        f"KG Level 3: {len(result['patterns'])} паттернов, "
        f"{result['cross_links']} cross-day связей"
    )
    return result


# ── Главный пайплайн ──


async def nightly_graph_cycle(
    agent_dir: str,
    config: dict | None = None,
    date: datetime | None = None,
) -> dict:
    """
    Ночной пайплайн Knowledge Graph.

    Последовательно запускает 3 уровня:
    1. Линковка (всегда)
    2. Саммари (всегда)
    3. Синтез (по расписанию: ежедневно → каждые N дней)

    Args:
        agent_dir: путь к директории агента
        config: knowledge_graph секция из agent.yaml
        date: дата для обработки (по умолчанию — сегодня)

    Returns:
        dict с результатами каждого уровня
    """
    if config is None:
        config = {}

    model = config.get("model", "haiku")

    result = {
        "level1": {},
        "level2": {},
        "level3": {},
        "level3_skipped": False,
    }

    logger.info(f"Knowledge Graph: начинаю ночной цикл для {agent_dir}")

    # ── Уровень 1: Линковка ──
    try:
        result["level1"] = await link_daily_entities(
            agent_dir, model=model, date=date
        )
    except Exception as e:
        logger.error(f"KG Level 1 failed: {e}")
        result["level1"] = {"ok": False, "error": str(e)}

    # ── Уровень 2: Саммари ──
    try:
        result["level2"] = await summarize_day(
            agent_dir, model=model, date=date
        )
    except Exception as e:
        logger.error(f"KG Level 2 failed: {e}")
        result["level2"] = {"ok": False, "error": str(e)}

    # ── Уровень 3: Синтез (по расписанию) ──
    if should_run_synthesis(agent_dir, config):
        try:
            result["level3"] = await synthesize_graph(
                agent_dir, model=model,
                max_summaries=config.get("max_summaries", 30),
            )
        except Exception as e:
            logger.error(f"KG Level 3 failed: {e}")
            result["level3"] = {"ok": False, "error": str(e)}
    else:
        result["level3_skipped"] = True
        state = _load_synthesis_state(agent_dir)
        logger.info(
            f"KG Level 3: пропущен (последний: {state.get('last_synthesis', 'никогда')}, "
            f"всего: {state.get('total_runs', 0)} запусков)"
        )

    # ── Lint: проверки целостности после KG-цикла (Этап 5) ──
    try:
        from .wiki_lint import run_lint
        lint_report = run_lint(agent_dir)
        result["lint"] = {
            "total": lint_report.total,
            "errors": lint_report.errors,
        }
    except Exception as e:
        logger.error(f"wiki_lint failed: {e}")
        result["lint"] = {"ok": False, "error": str(e)}

    # Git commit всех изменений
    memory.git_commit(
        agent_dir,
        f"Knowledge Graph: L1={result['level1'].get('links_found', 0)} links, "
        f"L2={'ok' if result['level2'].get('ok') else 'skip'}, "
        f"L3={'ok' if result['level3'].get('ok') else 'skip'}, "
        f"lint={result.get('lint', {}).get('total', '?')}",
    )

    logger.info(f"Knowledge Graph: ночной цикл завершён")
    return result


# ── Одноразовый backfill после обновления ──
#
# 2-фазный: линковка (L1+L2 по истории) и синтез (L3 по накопленным саммари).
# Каждая фаза — со своим маркером, независимо идемпотентна.
#
# Маркеры должны обновляться при изменении схемы KG. Текущая версия = v1
# (фильтр + 9 типов + supersession + lint + synthesis).

BACKFILL_MARKER = "sessions/.kg_backfill_v1_done"  # линковка (L1+L2)
BACKFILL_SYNTHESIS_MARKER = "sessions/.kg_backfill_synthesis_v1_done"  # L3
BACKFILL_DEFAULT_DAYS = 14


async def _backfill_linking_phase(
    agent_dir: str,
    config: dict,
    days: int,
) -> dict:
    """Фаза 1: прогнать L1+L2 по последним `days` дневным логам."""
    memory_path = memory.get_memory_path(agent_dir)
    marker = memory_path / BACKFILL_MARKER

    if marker.exists():
        return {"skipped": True, "reason": "already_done"}

    model = config.get("model", "haiku")

    daily_dir = memory_path / "daily"
    if not daily_dir.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().isoformat(), encoding="utf-8")
        return {"skipped": True, "reason": "no_daily_dir"}

    today = datetime.now().date()
    dates_to_process: list[datetime] = []
    for i in range(1, days + 1):
        d = today - timedelta(days=i)
        daily_file = daily_dir / f"{d.isoformat()}.md"
        if daily_file.exists():
            dates_to_process.append(
                datetime.combine(d, datetime.min.time())
            )

    if not dates_to_process:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().isoformat(), encoding="utf-8")
        return {
            "skipped": True,
            "reason": "no_historical_dailies",
            "processed": 0,
        }

    logger.info(
        f"KG backfill v1 (linking): запускаю для {len(dates_to_process)} "
        f"исторических дней ({agent_dir})"
    )

    processed = 0
    errors = 0
    for date in dates_to_process:
        try:
            await link_daily_entities(agent_dir, model=model, date=date)
            await summarize_day(agent_dir, model=model, date=date)
            processed += 1
        except Exception as e:
            logger.error(
                f"KG backfill: ошибка для {date.strftime('%Y-%m-%d')}: {e}"
            )
            errors += 1

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "completed_at": datetime.now().isoformat(),
                "processed": processed,
                "errors": errors,
                "days_window": days,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    logger.info(
        f"KG backfill v1 (linking): завершён — {processed} дней, "
        f"{errors} ошибок"
    )
    return {
        "skipped": False,
        "processed": processed,
        "errors": errors,
    }


async def _backfill_synthesis_phase(
    agent_dir: str,
    config: dict,
) -> dict:
    """
    Фаза 2: прогнать L3 (synthesize_graph) один раз по накопленным саммари.

    Отдельный маркер, так что у пользователей, которые уже прошли v1
    линковку, при следующем старте запустится только синтез — без
    повторного L1/L2 и траты токенов.
    """
    memory_path = memory.get_memory_path(agent_dir)
    marker = memory_path / BACKFILL_SYNTHESIS_MARKER

    if marker.exists():
        return {"skipped": True, "reason": "already_done"}

    model = config.get("model", "haiku")
    max_summaries = config.get("max_summaries", 30)

    # Предварительная проверка: есть ли вообще саммари для синтеза?
    summaries_dir = memory_path / SUMMARIES_DIR
    if not summaries_dir.exists() or len(list(summaries_dir.glob("*.md"))) < 2:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "completed_at": datetime.now().isoformat(),
                    "skipped_reason": "insufficient_summaries",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "skipped": True,
            "reason": "insufficient_summaries",
        }

    logger.info(
        f"KG backfill v1 (synthesis): запускаю L3 для {agent_dir}"
    )

    ok = False
    error_msg = ""
    try:
        result = await synthesize_graph(
            agent_dir, model=model, max_summaries=max_summaries
        )
        ok = bool(result.get("ok"))
    except Exception as e:
        logger.error(f"KG backfill synthesis: ошибка L3: {e}")
        error_msg = str(e)

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "completed_at": datetime.now().isoformat(),
                "ok": ok,
                "error": error_msg,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    logger.info(
        f"KG backfill v1 (synthesis): завершён (ok={ok})"
    )
    return {"skipped": False, "ok": ok, "error": error_msg}


async def maybe_backfill(
    agent_dir: str,
    config: dict | None = None,
    days: int = BACKFILL_DEFAULT_DAYS,
) -> dict:
    """
    Двухфазный одноразовый backfill после апдейта KG-схемы.

    Фаза 1 (linking): L1+L2 по последним `days` дням.
    Фаза 2 (synthesis): L3 по накопленным саммари.

    Каждая фаза — с отдельным маркером. Идемпотентны независимо, поэтому
    пользователи, которые ранее прошли только линковку, при следующем
    апдейте получат только синтез, без траты токенов на L1/L2.

    Returns:
        dict вида
        {
            "linking": {...},
            "synthesis": {...},
            "skipped": bool,  # True только если обе фазы skipped
        }
    """
    if config is None:
        config = {}

    linking_result = await _backfill_linking_phase(agent_dir, config, days)
    synthesis_result = await _backfill_synthesis_phase(agent_dir, config)

    # Lint и git-commit общие — после обеих фаз
    did_work = not (linking_result.get("skipped") and synthesis_result.get("skipped"))

    if did_work:
        try:
            from .wiki_lint import run_lint
            run_lint(agent_dir)
        except Exception as e:
            logger.warning(f"KG backfill: lint упал: {e}")

        try:
            memory.git_commit(
                agent_dir,
                (
                    f"KG backfill v1: "
                    f"linking={linking_result.get('processed', 0)}, "
                    f"synthesis={'ok' if synthesis_result.get('ok') else 'skip'}"
                ),
            )
        except Exception as e:
            logger.warning(f"KG backfill: git_commit упал: {e}")

    return {
        "linking": linking_result,
        "synthesis": synthesis_result,
        "skipped": (
            linking_result.get("skipped", False)
            and synthesis_result.get("skipped", False)
        ),
    }


async def nightly_graph_loop(
    agent_dir: str,
    config: dict | None = None,
    run_hour: int = 1,
    run_minute: int = 0,
) -> None:
    """
    Бесконечный цикл: запускает nightly_graph_cycle каждый день в указанное время.

    Args:
        agent_dir: путь к директории агента
        config: knowledge_graph секция из agent.yaml
        run_hour: час запуска (UTC, по умолчанию 1 = 04:00 MSK)
        run_minute: минута запуска
    """
    logger.info(
        f"KG nightly loop запущен для {agent_dir}, "
        f"расписание: {run_hour:02d}:{run_minute:02d} UTC"
    )

    # Одноразовый backfill после апдейта схемы KG. Срабатывает один раз
    # (идемпотентен через sessions/.kg_backfill_v1_done), затем больше не
    # беспокоит. Для других пользователей после update.sh — их исторические
    # daily-логи пройдут через новый фильтр и типизацию без ручных команд.
    try:
        backfill_result = await maybe_backfill(agent_dir, config)
        if not backfill_result.get("skipped"):
            logger.info(f"KG backfill применён: {backfill_result}")
    except Exception as e:
        logger.error(f"KG backfill упал на старте: {e}")

    while True:
        try:
            # Вычислить время до следующего запуска
            now = datetime.now()
            target = now.replace(
                hour=run_hour, minute=run_minute, second=0, microsecond=0
            )
            if target <= now:
                target += timedelta(days=1)

            sleep_seconds = (target - now).total_seconds()
            logger.debug(
                f"KG: следующий запуск через {sleep_seconds / 3600:.1f}ч "
                f"({target.strftime('%Y-%m-%d %H:%M')})"
            )
            await asyncio.sleep(sleep_seconds)

            # Запустить пайплайн
            result = await nightly_graph_cycle(agent_dir, config)
            logger.info(f"KG nightly result: {result}")

        except asyncio.CancelledError:
            logger.info("KG nightly loop остановлен")
            break
        except Exception as e:
            logger.error(f"KG nightly loop error: {e}")
            # Ждать до следующего дня при ошибке
            await asyncio.sleep(3600)


# ── Поиск с учётом графа ──


def get_related_by_graph(
    agent_dir: str, entity_name: str, max_results: int = 5
) -> list[dict]:
    """
    Найти связанные сущности через граф.

    Args:
        entity_name: имя сущности (например "Иван" или "Acme Corp")
        max_results: максимум связей

    Returns:
        [{name: str, type: str, context: str, strength: int}, ...]
    """
    graph = _load_graph(agent_dir)
    related = []
    name_lower = entity_name.lower()

    for edge in graph.get("edges", []):
        from_name = edge.get("from", "")
        to_name = edge.get("to", "")

        if name_lower in from_name.lower():
            related.append({
                "name": to_name,
                "type": edge.get("type", "related"),
                "context": edge.get("context", ""),
                "strength": edge.get("strength", 1),
                "last_seen": edge.get("last_seen", ""),
            })
        elif name_lower in to_name.lower():
            related.append({
                "name": from_name,
                "type": edge.get("type", "related"),
                "context": edge.get("context", ""),
                "strength": edge.get("strength", 1),
                "last_seen": edge.get("last_seen", ""),
            })

    # Сортировка по силе связи
    related.sort(key=lambda x: x["strength"], reverse=True)
    return related[:max_results]


# ── Fallback-промпты (если шаблоны не найдены) ──


_LEVEL1_FALLBACK = """# Knowledge Graph — Уровень 1: Линковка

Ты — фоновый процесс памяти. Проанализируй дневной лог и найди связи.

## Дневной лог ({date})

{daily_content}

## Существующие wiki-страницы

{existing_pages}

## Задача

1. Найди ВСЕ упомянутые сущности: люди, компании, проекты, концепции, продукты
2. Определи связи между ними (кто с кем, что с чем связано)
3. Для каждой связи укажи тип и контекст

## Формат ответа — СТРОГО JSON:

```json
{{
  "entities": [
    {{"name": "Иван", "category": "person"}},
    {{"name": "Acme Corp", "category": "company"}}
  ],
  "links": [
    {{
      "from": "Иван",
      "to": "Acme Corp",
      "type": "works_at",
      "context": "обсуждение стратегии"
    }}
  ]
}}
```

Типы связей: works_at, works_with, related_to, part_of, decided, discussed, blocked_by, depends_on

Если сущностей или связей нет — верни пустые массивы.
"""

_LEVEL2_FALLBACK = """# Knowledge Graph — Уровень 2: Итоги дня

Ты — фоновый процесс памяти. Создай структурированное саммари дня.

## Дневной лог ({date})

{daily_content}

## Задача

Проанализируй лог и создай краткое саммари:
1. Ключевые темы дня (что обсуждалось)
2. Принятые решения (если есть)
3. Действия на завтра (если упоминались)
4. Общее резюме (2-3 предложения)

Используй имена сущностей как они есть (для [[ссылок]]).

## Формат ответа — СТРОГО JSON:

```json
{{
  "summary": "Краткое описание дня в 2-3 предложениях",
  "topics": [
    {{"name": "Запуск продукта", "mentions": 3}},
    {{"name": "Найм команды", "mentions": 1}}
  ],
  "decisions": [
    {{
      "description": "Решили запустить MVP до конца апреля",
      "related": ["ProductX", "MVP"]
    }}
  ],
  "action_items": [
    "Подготовить презентацию для инвесторов",
    "Ревью дизайна лендинга"
  ]
}}
```

Если ничего значимого не произошло — верни минимальный JSON с пустыми массивами.
"""

_LEVEL3_FALLBACK = """# Knowledge Graph — Уровень 3: Синтез

Ты — фоновый процесс памяти с доступом к инструментам.
Проанализируй все саммари за период и найди глубинные связи.

## Саммари за {summary_count} дней

{summaries}

## Текущие synthesis-страницы

{existing_synthesis}

## Текущий граф (top-10 связей по силе)

{graph_summary}

## Задача

1. Найди повторяющиеся темы (что обсуждается регулярно?)
2. Проследи эволюцию решений (что менялось со временем?)
3. Выяви кластеры связей (группы связанных сущностей)
4. Обнови или создай synthesis-страницы:
   - `wiki/synthesis/recurring-themes.md` — повторяющиеся темы
   - `wiki/synthesis/decision-timeline.md` — хронология решений
   - `wiki/synthesis/knowledge-clusters.md` — кластеры знаний

Используй инструменты Read/Write/Edit для обновления файлов.

После обновления файлов, верни JSON с результатами:

```json
{{
  "patterns": [
    {{"theme": "Продукт X", "frequency": 5, "trend": "растёт"}},
    {{"theme": "Найм", "frequency": 3, "trend": "стабильно"}}
  ],
  "cross_links": [
    {{
      "from": "ProductX",
      "to": "Найм",
      "type": "cross_day",
      "context": "Для запуска ProductX нужна команда",
      "strength": 3,
      "first_seen": "2026-04-01"
    }}
  ]
}}
```
"""
