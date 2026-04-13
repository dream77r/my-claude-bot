"""
Тесты для src/mcp_skill_marketplace.py.

Стратегия: обработчики тестируем напрямую через make_handlers() без поднятия
MCP-транспорта. Пул — фейковый, созданный make_fake_pool() из test_skill_pool.
"""

import json
from pathlib import Path

import pytest

from src.mcp_skill_marketplace import (
    ALLOWED_TOOL_NAMES,
    SERVER_NAME,
    build_skill_marketplace_server,
    make_handlers,
)
from tests.test_skill_pool import (
    make_agent_dir,
    make_fake_pool,
    make_pool_pointing_to_fake,
)


# ───────────────────────── Helpers ─────────────────────────


def make_fake_pool_with_bundle(tmp_path: Path) -> Path:
    """
    Фейковый пул с тремя скиллами:
    - web-research (single, safe)
    - task-tracking (single, safe, requires memory)
    - garmin-pulse (bundle, has_scripts=true)
    """
    pool_repo = make_fake_pool(tmp_path)

    # Добавим bundle со скриптами
    bundle_dir = pool_repo / "published" / "garmin-pulse"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "SKILL.md").write_text(
        "---\nname: garmin-pulse\nversion: 1.0.0\n"
        "description: Garmin sync\n---\n# Garmin\n",
        encoding="utf-8",
    )
    scripts_dir = bundle_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "sync.py").write_text("print('sync')", encoding="utf-8")

    # Обновим manifest
    manifest = json.loads((pool_repo / "manifest.json").read_text())
    manifest["skills"]["garmin-pulse"] = {
        "path": "published/garmin-pulse",
        "type": "bundle",
        "title": "Garmin Pulse",
        "description": "Garmin sync",
        "version": "1.0.0",
        "tags": ["fitness"],
        "requires_memory": [],
        "has_scripts": True,
        "author": "dream",
        "created": "2026-04-12",
    }
    (pool_repo / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return pool_repo


# ───────────────────────── list_pool_skills ─────────────────


class TestListPoolSkills:
    @pytest.mark.asyncio
    async def test_lists_all_skills(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path, "worker1")

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["list_pool_skills"]({})

        assert "is_error" not in result or result["is_error"] is False
        text = result["content"][0]["text"]
        assert "web-research" in text
        assert "task-tracking" in text
        assert "garmin-pulse" in text
        assert "⚠️ scripts" in text  # предупреждение про bundle

    @pytest.mark.asyncio
    async def test_empty_pool(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        # Обнуляем manifest до пустого
        (fake / "manifest.json").write_text(
            json.dumps({"version": "1.0", "skills": {}}),
            encoding="utf-8",
        )
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["list_pool_skills"]({})

        assert "Пул пуст" in result["content"][0]["text"]


# ───────────────────────── search_pool_skills ──────────────


class TestSearchPoolSkills:
    @pytest.mark.asyncio
    async def test_search_by_name(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["search_pool_skills"]({"query": "web"})

        text = result["content"][0]["text"]
        assert "web-research" in text
        assert "task-tracking" not in text
        assert "garmin-pulse" not in text

    @pytest.mark.asyncio
    async def test_search_by_tag(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["search_pool_skills"]({"query": "fitness"})

        assert "garmin-pulse" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_search_by_description(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["search_pool_skills"]({"query": "команды"})

        assert "task-tracking" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_search_empty_query_errors(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["search_pool_skills"]({"query": "   "})

        assert result.get("is_error") is True

    @pytest.mark.asyncio
    async def test_search_no_matches(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["search_pool_skills"]({"query": "zzz-nothing"})

        assert "ничего не найдено" in result["content"][0]["text"]


# ───────────────────────── install_skill_from_pool ──────────


class TestInstallSkillFromPool:
    @pytest.mark.asyncio
    async def test_installs_safe_single_skill(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path, "worker1")

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["install_skill_from_pool"]({"name": "web-research"})

        assert result.get("is_error") is not True
        assert "установлен" in result["content"][0]["text"]
        # Файл реально создан в agent_dir/skills/
        installed = agent_dir / "skills" / "web-research.md"
        assert installed.exists()
        assert "Web Research" in installed.read_text()

    @pytest.mark.asyncio
    async def test_refuses_bundle_with_scripts(self, tmp_path):
        """STRICT GUARD: воркер не может установить bundle с has_scripts=true."""
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path, "worker1")

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["install_skill_from_pool"]({"name": "garmin-pulse"})

        assert result.get("is_error") is True
        text = result["content"][0]["text"]
        assert "has_scripts" in text or "скрипт" in text
        assert "master" in text or "владельца" in text
        # Bundle НЕ установлен
        assert not (agent_dir / "skills" / "garmin-pulse").exists()

    @pytest.mark.asyncio
    async def test_unknown_skill_errors(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["install_skill_from_pool"]({"name": "nonexistent"})

        assert result.get("is_error") is True
        assert "не найден" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_empty_name_errors(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        handlers = make_handlers(agent_dir, pool)
        result = await handlers["install_skill_from_pool"]({"name": ""})

        assert result.get("is_error") is True

    @pytest.mark.asyncio
    async def test_already_installed_errors(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path, "worker1")

        handlers = make_handlers(agent_dir, pool)
        # Первая установка OK
        await handlers["install_skill_from_pool"]({"name": "web-research"})
        # Вторая должна упасть
        result = await handlers["install_skill_from_pool"]({"name": "web-research"})

        assert result.get("is_error") is True
        assert "уже установлен" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_target_dir_is_closure_locked(self, tmp_path):
        """
        Критичная проверка границы безопасности: target_dir захвачен в closure
        при создании handlers и НЕ может быть подменён через args. Даже если
        LLM передаст лишние поля, install идёт только в исходный agent_dir.
        """
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        worker_dir = make_agent_dir(tmp_path, "worker1")
        other_dir = make_agent_dir(tmp_path, "other_victim")

        handlers = make_handlers(worker_dir, pool)
        # Попытка атаки: лишние поля в args игнорируются
        result = await handlers["install_skill_from_pool"]({
            "name": "web-research",
            "target_dir": str(other_dir),  # должно быть проигнорировано
            "agent_dir": str(other_dir),
        })

        assert result.get("is_error") is not True
        # Установлен у worker1, НЕ у other_victim
        assert (worker_dir / "skills" / "web-research.md").exists()
        assert not (other_dir / "skills" / "web-research.md").exists()


# ───────────────────────── build_skill_marketplace_server ──


class TestBuildServer:
    def test_returns_none_when_pool_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKILL_POOL_URL", "disabled")
        agent_dir = make_agent_dir(tmp_path, "worker1")

        server = build_skill_marketplace_server(agent_dir)

        assert server is None

    def test_builds_server_with_explicit_pool(self, tmp_path):
        fake = make_fake_pool_with_bundle(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path, "worker1")

        server = build_skill_marketplace_server(agent_dir, pool=pool)

        assert server is not None

    def test_allowed_tool_names_match_server(self):
        """ALLOWED_TOOL_NAMES должны соответствовать SERVER_NAME + tool names."""
        assert len(ALLOWED_TOOL_NAMES) == 3
        for name in ALLOWED_TOOL_NAMES:
            assert name.startswith(f"mcp__{SERVER_NAME}__")
        tool_suffixes = [n.split("__")[-1] for n in ALLOWED_TOOL_NAMES]
        assert set(tool_suffixes) == {
            "list_pool_skills",
            "search_pool_skills",
            "install_skill_from_pool",
        }
