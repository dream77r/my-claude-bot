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


async def _call_claude_simple(
    prompt: str,
    model: str = "haiku",
    cwd: str | None = None,
) -> str:
    """Простой LLM-вызов (без tools)."""
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
    model: str = "haiku",
    cwd: str | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """Агентный вызов Claude (с tools)."""
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


def _load_template(agent_dir: str, template_name: str) -> str:
    """Загрузить промпт-шаблон из templates/."""
    path = Path(agent_dir) / "templates" / template_name
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning(f"Шаблон {template_name} не найден")
    return ""


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
            }
            # Проверить дубликат (та же пара + тот же день)
            is_dup = any(
                e.get("from") == edge["from"]
                and e.get("to") == edge["to"]
                and e.get("date") == edge["date"]
                for e in graph["edges"]
            )
            if not is_dup:
                # Обновить strength для существующих рёбер
                updated = False
                for existing in graph["edges"]:
                    if (
                        existing.get("from") == edge["from"]
                        and existing.get("to") == edge["to"]
                    ) or (
                        existing.get("from") == edge["to"]
                        and existing.get("to") == edge["from"]
                    ):
                        existing["strength"] = existing.get("strength", 1) + 1
                        existing["last_seen"] = date_str
                        existing["context"] = edge["context"]
                        updated = True
                        break
                if not updated:
                    edge["first_seen"] = date_str
                    edge["last_seen"] = date_str
                    edge["strength"] = 1
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
        for link in cross_links:
            edge = {
                "from": link.get("from", ""),
                "to": link.get("to", ""),
                "type": link.get("type", "cross_day"),
                "context": link.get("context", ""),
                "date": datetime.now().strftime("%Y-%m-%d"),
                "first_seen": link.get("first_seen", ""),
                "last_seen": datetime.now().strftime("%Y-%m-%d"),
                "strength": link.get("strength", 1),
            }
            # Проверить дубликат
            is_dup = any(
                (e.get("from") == edge["from"] and e.get("to") == edge["to"])
                or (e.get("from") == edge["to"] and e.get("to") == edge["from"])
                for e in graph["edges"]
                if e.get("type") == "cross_day"
            )
            if not is_dup:
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

    # Git commit всех изменений
    memory.git_commit(
        agent_dir,
        f"Knowledge Graph: L1={result['level1'].get('links_found', 0)} links, "
        f"L2={'ok' if result['level2'].get('ok') else 'skip'}, "
        f"L3={'ok' if result['level3'].get('ok') else 'skip'}",
    )

    logger.info(f"Knowledge Graph: ночной цикл завершён")
    return result


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
