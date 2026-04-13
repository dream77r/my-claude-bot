"""
Skill Marketplace MCP — in-process MCP сервер для worker-агентов.

Даёт воркерам возможность САМОСТОЯТЕЛЬНО устанавливать скиллы из публичного
пула, НЕ давая им права создавать новые скиллы и НЕ давая произвольного доступа
к файловой системе вне их песочницы.

Граница безопасности:
1. Источник скиллов жёстко прибит к SkillPool из make_pool_from_env()
2. Целевая директория (agent_dir) захвачена в closure при инициализации —
   LLM не может её подменить, в tool args она даже не передаётся
3. Bundle со скриптами (has_scripts=true) воркеру ставить запрещено — только
   через master. Защищает от скачивания произвольного исполняемого кода
4. Нет create/delete/update операций — воркер может только читать каталог
   и ставить safe-скиллы

Tools:
- list_pool_skills()        — каталог
- search_pool_skills(query) — фильтр по name/description/tags
- install_skill_from_pool(name) — установка в свой agent_dir

Использование:
    from .mcp_skill_marketplace import build_skill_marketplace_server
    server = build_skill_marketplace_server(Path("/path/to/agents/coder"))
    if server:
        options.mcp_servers["skill_marketplace"] = server
"""

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import create_sdk_mcp_server, tool

from .skill_pool import SkillPool, make_pool_from_env

logger = logging.getLogger(__name__)

SERVER_NAME = "skill_marketplace"

HandlerFn = Callable[[dict], Awaitable[dict[str, Any]]]


def _text_response(text: str, is_error: bool = False) -> dict[str, Any]:
    """Сформировать ответ в формате MCP tool result."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result


def make_handlers(
    agent_dir: Path, pool: SkillPool
) -> dict[str, HandlerFn]:
    """
    Построить dict {tool_name: async handler} для skill marketplace.

    Выделено отдельно от create_sdk_mcp_server() чтобы тесты могли вызывать
    обработчики напрямую без поднятия MCP-транспорта.

    Args:
        agent_dir: корневая директория агента (захватывается в closure).
                   Используется как единственный target для install — LLM
                   не может её подменить, в tool args она не передаётся.
        pool: готовый SkillPool.

    Returns:
        dict с ключами list_pool_skills, search_pool_skills, install_skill_from_pool
    """
    agent_dir = Path(agent_dir).resolve()

    async def list_pool_skills(args: dict) -> dict[str, Any]:
        try:
            if not pool.is_available():
                pool.refresh()
            entries = pool.list_skills()
        except Exception as e:
            logger.warning(f"list_pool_skills failed: {e}")
            return _text_response(f"Ошибка чтения пула: {e}", is_error=True)

        if not entries:
            return _text_response("Пул пуст.")

        lines = [f"В пуле доступно {len(entries)} скиллов:", ""]
        for e in entries:
            icon = "📦" if e.type == "bundle" else "📄"
            warn = " ⚠️ scripts" if e.has_scripts else ""
            lines.append(f"{icon} **{e.name}** v{e.version}{warn}")
            if e.description:
                lines.append(f"   {e.description}")
            if e.tags:
                lines.append(f"   tags: {', '.join(e.tags)}")
            lines.append("")
        lines.append(
            "Для установки: install_skill_from_pool(name=\"<имя>\"). "
            "Скиллы со ⚠️ scripts тебе недоступны — попроси master-агента."
        )
        return _text_response("\n".join(lines))

    async def search_pool_skills(args: dict) -> dict[str, Any]:
        query = (args.get("query") or "").strip().lower()
        if not query:
            return _text_response(
                "Укажи непустой query для поиска.", is_error=True
            )

        try:
            if not pool.is_available():
                pool.refresh()
            entries = pool.list_skills()
        except Exception as e:
            logger.warning(f"search_pool_skills failed: {e}")
            return _text_response(f"Ошибка чтения пула: {e}", is_error=True)

        matches = []
        for e in entries:
            haystack = " ".join([
                e.name.lower(),
                e.description.lower(),
                " ".join(t.lower() for t in e.tags),
            ])
            if query in haystack:
                matches.append(e)

        if not matches:
            return _text_response(f"По запросу '{query}' ничего не найдено.")

        lines = [f"Найдено {len(matches)} скиллов по '{query}':", ""]
        for e in matches:
            icon = "📦" if e.type == "bundle" else "📄"
            warn = " ⚠️ scripts" if e.has_scripts else ""
            lines.append(
                f"{icon} **{e.name}** v{e.version}{warn} — {e.description}"
            )
        return _text_response("\n".join(lines))

    async def install_skill_from_pool(args: dict) -> dict[str, Any]:
        name = (args.get("name") or "").strip()
        if not name:
            return _text_response("Укажи name скилла.", is_error=True)

        try:
            if not pool.is_available():
                pool.refresh()
            entry = pool.get_skill(name)
        except Exception as e:
            logger.warning(f"install_skill_from_pool lookup failed: {e}")
            return _text_response(f"Ошибка чтения пула: {e}", is_error=True)

        if entry is None:
            return _text_response(
                f"Скилл '{name}' не найден в пуле. "
                f"Посмотри доступные через list_pool_skills.",
                is_error=True,
            )

        # STRICT GUARD: воркер не ставит bundle с исполняемыми скриптами
        if entry.has_scripts:
            logger.info(
                f"skill_marketplace: воркер {agent_dir.name} пытался поставить "
                f"bundle '{name}' со скриптами — заблокировано"
            )
            return _text_response(
                f"Скилл '{name}' содержит исполняемые скрипты "
                f"(has_scripts=true). Воркерам такие скиллы ставить нельзя — "
                f"попроси master-агента через bus или владельца установить его "
                f"командой /installskill {name} @{agent_dir.name}.",
                is_error=True,
            )

        try:
            result = pool.install_skill(name, agent_dir)
        except Exception as e:
            logger.warning(f"install_skill_from_pool install failed: {e}")
            return _text_response(f"Ошибка установки: {e}", is_error=True)

        if not result.ok:
            return _text_response(
                f"Не удалось установить '{name}': {result.error}",
                is_error=True,
            )

        logger.info(
            f"skill_marketplace: воркер {agent_dir.name} установил "
            f"скилл '{name}' → {result.installed_to}"
        )
        msg = (
            f"✅ Скилл '{name}' установлен в {result.installed_to}. "
            f"Будет доступен при следующем сообщении."
        )
        if result.missing_memory:
            msg += (
                f"\n\nВнимание: скиллу нужны файлы памяти, которых у тебя нет: "
                f"{', '.join(result.missing_memory)}. Создай их сам или "
                f"попроси владельца."
            )
        return _text_response(msg)

    return {
        "list_pool_skills": list_pool_skills,
        "search_pool_skills": search_pool_skills,
        "install_skill_from_pool": install_skill_from_pool,
    }


def build_skill_marketplace_server(
    agent_dir: Path,
    project_root: Path | None = None,
    pool: SkillPool | None = None,
):
    """
    Собрать in-process MCP сервер для одного worker-агента.

    Args:
        agent_dir: корневая директория агента (содержит skills/, memory/).
        project_root: корень проекта для make_pool_from_env(). Если None,
                      вычисляется как agent_dir.parent.parent.
        pool: готовый SkillPool (для тестов). Если None — создаётся из env.

    Returns:
        McpSdkServerConfig готовый к подключению в options.mcp_servers,
        либо None если пул отключён (SKILL_POOL_URL=disabled).
    """
    agent_dir = Path(agent_dir).resolve()

    if pool is None:
        if project_root is None:
            project_root = agent_dir.parent.parent
        pool = make_pool_from_env(project_root)
        if pool is None:
            logger.info(
                f"skill_marketplace MCP: пул отключён, сервер не создаётся "
                f"(agent_dir={agent_dir})"
            )
            return None

    handlers = make_handlers(agent_dir, pool)

    list_tool = tool(
        "list_pool_skills",
        (
            "List all skills available in the public skill marketplace pool. "
            "Returns name, description, type (single/bundle), and whether the "
            "skill contains executable scripts. Use this to browse what you "
            "can install before calling install_skill_from_pool."
        ),
        {},
    )(handlers["list_pool_skills"])

    search_tool = tool(
        "search_pool_skills",
        (
            "Search the skill marketplace pool by keyword. Matches against "
            "skill name, description, and tags (case-insensitive). Returns "
            "skills that contain the query string in any of those fields."
        ),
        {"query": str},
    )(handlers["search_pool_skills"])

    install_tool = tool(
        "install_skill_from_pool",
        (
            "Install a skill from the public marketplace pool into your own "
            "agent directory. Only safe skills (no executable scripts) are "
            "allowed for workers — bundles with scripts must be installed by "
            "the master agent. The target directory is fixed to your own "
            "agent folder and cannot be overridden."
        ),
        {"name": str},
    )(handlers["install_skill_from_pool"])

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[list_tool, search_tool, install_tool],
    )


# Имена tools в формате Claude Code CLI allowed_tools: mcp__<server>__<tool>
ALLOWED_TOOL_NAMES = [
    f"mcp__{SERVER_NAME}__list_pool_skills",
    f"mcp__{SERVER_NAME}__search_pool_skills",
    f"mcp__{SERVER_NAME}__install_skill_from_pool",
]
