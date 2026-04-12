"""
Тесты для src/skill_pool.py.

Стратегия: вместо клонирования реального git-репо создаём "поддельный пул"
на диске в tmp_path и указываем на него как на локальный каталог. Это
позволяет тестировать всю логику (read_manifest, list_skills, install_skill,
check_memory_for_skill) без сетевых зависимостей.

Тесты, которые реально ходят в git, помечены skip по умолчанию.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.skill_pool import (
    InstallResult,
    SkillCatalogEntry,
    SkillPool,
    SkillPoolError,
    extract_skill_metadata,
    make_pool_from_env,
)


# ───────────────────────── Helpers ─────────────────────────


def make_fake_pool(tmp_path: Path) -> Path:
    """
    Создать фейковый пул в tmp_path с manifest.json и двумя скиллами.
    Возвращает путь к "репо" (директория которую SkillPool ожидает).
    """
    pool_repo = tmp_path / "pool" / "my-claude-bot-skills"
    pool_repo.mkdir(parents=True)
    (pool_repo / "published").mkdir()
    (pool_repo / "incoming").mkdir()

    # Первый скилл — без требований к памяти
    (pool_repo / "published" / "web-research.md").write_text(
        "---\n"
        "name: web-research\n"
        "version: 1.0.0\n"
        'description: "Поиск информации в интернете"\n'
        "license: MIT\n"
        'when_to_use: "When user asks to find info"\n'
        "triggers:\n"
        "  keywords: ['найди', 'поищи']\n"
        "tags: [research, web]\n"
        "requires_memory: []\n"
        "always: false\n"
        "---\n"
        "# Skill: Web Research\nТело скилла.\n",
        encoding="utf-8",
    )

    # Второй скилл — требует файл памяти
    (pool_repo / "published" / "task-tracking.md").write_text(
        "---\n"
        "name: task-tracking\n"
        "version: 1.2.0\n"
        'description: "Трекинг задач команды"\n'
        "license: MIT\n"
        'when_to_use: "When someone creates or updates a task"\n'
        "triggers:\n"
        "  keywords: ['задача', 'todo']\n"
        "tags: [tasks, team]\n"
        'requires_memory: ["wiki/concepts/tasks.md"]\n'
        "always: true\n"
        "---\n"
        "# Skill: Task Tracking\nТело скилла.\n",
        encoding="utf-8",
    )

    # Скилл в incoming — НЕ должен попадать в каталог
    (pool_repo / "incoming" / "experimental.md").write_text(
        "---\nname: experimental\nversion: 0.1.0\n---\n# WIP\n",
        encoding="utf-8",
    )

    manifest = {
        "version": "1.0",
        "updated": "2026-04-12T00:00:00Z",
        "skills": {
            "web-research": {
                "file": "published/web-research.md",
                "title": "Web Research",
                "description": "Поиск информации в интернете",
                "version": "1.0.0",
                "tags": ["research", "web"],
                "requires_memory": [],
                "author": "dream",
                "created": "2026-04-12",
            },
            "task-tracking": {
                "file": "published/task-tracking.md",
                "title": "Task Tracking",
                "description": "Трекинг задач команды",
                "version": "1.2.0",
                "tags": ["tasks", "team"],
                "requires_memory": ["wiki/concepts/tasks.md"],
                "author": "dream",
                "created": "2026-04-12",
            },
        },
    }
    (pool_repo / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return pool_repo


def make_agent_dir(tmp_path: Path, name: str = "test") -> Path:
    """Создать минимальную структуру агента."""
    agent_dir = tmp_path / "agents" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "skills").mkdir()
    (agent_dir / "memory").mkdir()
    return agent_dir


def make_pool_pointing_to_fake(tmp_path: Path, fake_repo: Path) -> SkillPool:
    """
    Создать SkillPool который "думает" что repo_dir уже клонирован — указываем
    cache_dir так, чтобы SkillPool._extract_repo_name совпал с fake_repo.
    """
    pool = SkillPool(
        pool_url=f"file://{fake_repo.parent}/my-claude-bot-skills.git",
        cache_dir=fake_repo.parent,
    )
    # Корректируем repo_dir вручную, чтобы совпадал с нашим fake
    pool.repo_dir = fake_repo
    return pool


# ───────────────────── SkillCatalogEntry ──────────────────


class TestSkillCatalogEntry:
    def test_from_dict_full(self):
        data = {
            "file": "published/x.md",
            "title": "Title",
            "description": "Desc",
            "version": "1.0.0",
            "tags": ["a", "b"],
            "requires_memory": ["wiki/x.md"],
            "author": "me",
            "created": "2026-04-12",
        }
        entry = SkillCatalogEntry.from_dict("x", data)
        assert entry.name == "x"
        assert entry.file == "published/x.md"
        assert entry.tags == ["a", "b"]
        assert entry.requires_memory == ["wiki/x.md"]

    def test_from_dict_minimal(self):
        entry = SkillCatalogEntry.from_dict("x", {})
        assert entry.name == "x"
        assert entry.version == "0.0.0"
        assert entry.tags == []
        assert entry.requires_memory == []


# ───────────────────── SkillPool construction ──────────────


class TestSkillPoolInit:
    def test_empty_url_raises(self, tmp_path):
        with pytest.raises(SkillPoolError):
            SkillPool(pool_url="", cache_dir=tmp_path)

    def test_extracts_repo_name_from_https(self, tmp_path):
        pool = SkillPool(
            pool_url="https://github.com/dream77r/my-claude-bot-skills.git",
            cache_dir=tmp_path,
        )
        assert pool.repo_dir.name == "my-claude-bot-skills"

    def test_extracts_repo_name_from_ssh(self, tmp_path):
        pool = SkillPool(
            pool_url="git@github.com:dream77r/my-claude-bot-skills.git",
            cache_dir=tmp_path,
        )
        assert pool.repo_dir.name == "my-claude-bot-skills"

    def test_extracts_repo_name_no_git_suffix(self, tmp_path):
        pool = SkillPool(
            pool_url="https://github.com/user/skills",
            cache_dir=tmp_path,
        )
        assert pool.repo_dir.name == "skills"


# ───────────────────── Manifest reading ────────────────────


class TestManifest:
    def test_is_available_false_when_no_repo(self, tmp_path):
        pool = SkillPool(pool_url="https://x/y.git", cache_dir=tmp_path)
        assert pool.is_available() is False

    def test_is_available_true_when_repo_and_manifest_exist(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        assert pool.is_available() is True

    def test_read_manifest_missing_raises(self, tmp_path):
        pool = SkillPool(pool_url="https://x/y.git", cache_dir=tmp_path)
        with pytest.raises(SkillPoolError, match="manifest.json не найден"):
            pool.read_manifest()

    def test_read_manifest_broken_json_raises(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        (fake / "manifest.json").write_text("{ broken json")
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        with pytest.raises(SkillPoolError, match="битый"):
            pool.read_manifest()

    def test_read_manifest_ok(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        manifest = pool.read_manifest()
        assert "skills" in manifest
        assert "web-research" in manifest["skills"]


# ───────────────────── list_skills / get_skill ─────────────


class TestListSkills:
    def test_lists_only_published(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        skills = pool.list_skills()
        names = [s.name for s in skills]
        assert "web-research" in names
        assert "task-tracking" in names
        # experimental в incoming — НЕ в каталоге
        assert "experimental" not in names

    def test_list_sorted_by_name(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        skills = pool.list_skills()
        assert [s.name for s in skills] == sorted(s.name for s in skills)

    def test_get_skill_found(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        entry = pool.get_skill("web-research")
        assert entry is not None
        assert entry.version == "1.0.0"
        assert entry.requires_memory == []

    def test_get_skill_not_found(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        assert pool.get_skill("nonexistent") is None


# ───────────────────── read_skill_body ─────────────────────


class TestReadSkillBody:
    def test_reads_published_skill(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        entry = pool.get_skill("web-research")
        body = pool.read_skill_body(entry)
        assert "name: web-research" in body
        assert "Тело скилла" in body

    def test_rejects_non_published_path(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        # Подделываем entry с файлом в incoming
        bad = SkillCatalogEntry(
            name="bad", file="incoming/bad.md", title="", description="",
            version="0.1.0",
        )
        with pytest.raises(SkillPoolError, match="не в published"):
            pool.read_skill_body(bad)

    def test_missing_file_raises(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        missing = SkillCatalogEntry(
            name="missing", file="published/missing.md", title="", description="",
            version="0.1.0",
        )
        with pytest.raises(SkillPoolError, match="не найден"):
            pool.read_skill_body(missing)


# ───────────────────── check_memory_for_skill ──────────────


class TestCheckMemoryForSkill:
    def test_no_requirements_returns_empty(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)
        entry = pool.get_skill("web-research")
        missing = pool.check_memory_for_skill(entry, agent_dir / "memory")
        assert missing == []

    def test_missing_file_reported(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)
        entry = pool.get_skill("task-tracking")
        missing = pool.check_memory_for_skill(entry, agent_dir / "memory")
        assert missing == ["wiki/concepts/tasks.md"]

    def test_present_file_not_reported(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)
        # Создаём требуемый файл
        tasks_path = agent_dir / "memory" / "wiki" / "concepts"
        tasks_path.mkdir(parents=True)
        (tasks_path / "tasks.md").write_text("# Tasks")
        entry = pool.get_skill("task-tracking")
        missing = pool.check_memory_for_skill(entry, agent_dir / "memory")
        assert missing == []


# ───────────────────── install_skill ───────────────────────


class TestInstallSkill:
    def test_installs_skill_without_memory_req(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        result = pool.install_skill("web-research", agent_dir)

        assert result.ok is True
        assert result.missing_memory == []
        assert Path(result.installed_to).exists()
        assert "web-research" in Path(result.installed_to).name

    def test_install_reports_missing_memory_but_still_installs(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        result = pool.install_skill("task-tracking", agent_dir)

        assert result.ok is True  # по умолчанию soft-проверка
        assert result.missing_memory == ["wiki/concepts/tasks.md"]
        assert (agent_dir / "skills" / "task-tracking.md").exists()

    def test_strict_memory_blocks_install_when_missing(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        result = pool.install_skill(
            "task-tracking", agent_dir, strict_memory=True
        )

        assert result.ok is False
        assert result.missing_memory == ["wiki/concepts/tasks.md"]
        assert not (agent_dir / "skills" / "task-tracking.md").exists()

    def test_install_unknown_skill_fails(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        result = pool.install_skill("nonexistent", agent_dir)

        assert result.ok is False
        assert "не найден" in result.error

    def test_install_existing_without_overwrite_fails(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        # Первая установка
        r1 = pool.install_skill("web-research", agent_dir)
        assert r1.ok is True

        # Повторная без overwrite — ошибка
        r2 = pool.install_skill("web-research", agent_dir)
        assert r2.ok is False
        assert "уже установлен" in r2.error

    def test_install_existing_with_overwrite_succeeds(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        pool.install_skill("web-research", agent_dir)
        r2 = pool.install_skill("web-research", agent_dir, overwrite=True)
        assert r2.ok is True

    def test_installed_content_matches_pool(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)

        pool.install_skill("web-research", agent_dir)
        installed = (agent_dir / "skills" / "web-research.md").read_text()
        original = (fake / "published" / "web-research.md").read_text()
        assert installed == original


# ───────────────────── uninstall_skill ─────────────────────


class TestUninstallSkill:
    def test_uninstall_existing(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)
        pool.install_skill("web-research", agent_dir)

        ok = pool.uninstall_skill("web-research", agent_dir)

        assert ok is True
        assert not (agent_dir / "skills" / "web-research.md").exists()

    def test_uninstall_nonexistent(self, tmp_path):
        fake = make_fake_pool(tmp_path)
        pool = make_pool_pointing_to_fake(tmp_path, fake)
        agent_dir = make_agent_dir(tmp_path)
        ok = pool.uninstall_skill("ghost", agent_dir)
        assert ok is False


# ───────────────────── make_pool_from_env ──────────────────


class TestMakePoolFromEnv:
    def test_returns_none_when_no_url(self, tmp_path):
        with patch.dict(os.environ, {"SKILL_POOL_URL": ""}, clear=False):
            pool = make_pool_from_env(tmp_path)
        assert pool is None

    def test_creates_pool_with_defaults(self, tmp_path):
        env = {
            "SKILL_POOL_URL": "https://github.com/user/skills.git",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("SKILL_POOL_BRANCH", None)
            os.environ.pop("SKILL_POOL_CACHE", None)
            pool = make_pool_from_env(tmp_path)
        assert pool is not None
        assert pool.pool_url == "https://github.com/user/skills.git"
        assert pool.branch == "main"

    def test_respects_branch_override(self, tmp_path):
        env = {
            "SKILL_POOL_URL": "https://github.com/user/skills.git",
            "SKILL_POOL_BRANCH": "develop",
        }
        with patch.dict(os.environ, env, clear=False):
            pool = make_pool_from_env(tmp_path)
        assert pool.branch == "develop"


# ───────────────────── extract_skill_metadata ──────────────


class TestExtractSkillMetadata:
    def test_extracts_full_meta(self, tmp_path):
        skill = tmp_path / "test-skill.md"
        skill.write_text(
            "---\n"
            "name: test-skill\n"
            "version: 2.0.0\n"
            'description: "Тестовый скилл"\n'
            "tags: [a, b, c]\n"
            "requires_memory: [wiki/x.md]\n"
            'author: "dream"\n'
            'created: "2026-04-12"\n'
            "---\n"
            "# Body\n",
            encoding="utf-8",
        )
        meta = extract_skill_metadata(skill)
        assert meta is not None
        assert meta["version"] == "2.0.0"
        assert meta["description"] == "Тестовый скилл"
        assert meta["tags"] == ["a", "b", "c"]
        assert meta["requires_memory"] == ["wiki/x.md"]
        assert meta["author"] == "dream"

    def test_returns_none_without_frontmatter(self, tmp_path):
        skill = tmp_path / "no-fm.md"
        skill.write_text("# No frontmatter\nContent.", encoding="utf-8")
        assert extract_skill_metadata(skill) is None

    def test_returns_none_on_bad_yaml(self, tmp_path):
        skill = tmp_path / "bad.md"
        skill.write_text(
            "---\n{{invalid yaml\n---\n# Body",
            encoding="utf-8",
        )
        assert extract_skill_metadata(skill) is None


# ───────────────────── Git integration (skipped by default) ─


@pytest.mark.skipif(
    not os.environ.get("SKILL_POOL_INTEGRATION_TEST"),
    reason="Требует сети и реального репо, запускается через SKILL_POOL_INTEGRATION_TEST=1",
)
class TestGitIntegration:
    """Тесты которые реально клонируют репо. По умолчанию пропущены."""

    def test_clone_public_repo(self, tmp_path):
        pool = SkillPool(
            pool_url="https://github.com/dream77r/my-claude-bot-skills.git",
            cache_dir=tmp_path,
        )
        pool.refresh()
        assert pool.is_available() is True
