"""Тесты для Skills с YAML frontmatter."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.agent import Agent


@pytest.fixture
def agent_with_skills(tmp_path):
    """Создать агента со скиллами с frontmatter."""
    agent_dir = tmp_path / "agents" / "test"
    agent_dir.mkdir(parents=True)

    # agent.yaml
    config = {
        "name": "test",
        "bot_token": "123:ABC",
        "skills": ["allowed-skill", "missing-env-skill"],
    }
    yaml_path = agent_dir / "agent.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(config, f)

    # skills/
    skills_dir = agent_dir / "skills"
    skills_dir.mkdir()

    # Скилл с frontmatter — разрешён
    (skills_dir / "allowed-skill.md").write_text(
        "---\n"
        'description: "Test skill"\n'
        "requirements:\n"
        "  commands: []\n"
        "  env: []\n"
        "always: false\n"
        "---\n"
        "# Allowed Skill\nТело скилла.\n"
    )

    # Скилл с always: true
    (skills_dir / "always-on.md").write_text(
        "---\n"
        'description: "Always on"\n'
        "always: true\n"
        "---\n"
        "# Always On\nВсегда активен.\n"
    )

    # Скилл с недостающей env переменной
    (skills_dir / "missing-env-skill.md").write_text(
        "---\n"
        'description: "Needs API key"\n'
        "requirements:\n"
        "  env: [SUPER_SECRET_KEY_XXXYYY]\n"
        "---\n"
        "# Missing Env Skill\nНе загрузится.\n"
    )

    # Скилл без frontmatter (legacy)
    (skills_dir / "legacy-skill.md").write_text(
        "# Legacy\nСтарый формат без frontmatter.\n"
    )

    # Скилл не в списке skills и не always
    (skills_dir / "not-listed.md").write_text(
        "---\n"
        'description: "Not listed"\n'
        "always: false\n"
        "---\n"
        "# Not Listed\nНе должен загрузиться.\n"
    )

    return Agent(str(yaml_path))


class TestParseFrontmatter:
    def test_parses_valid(self):
        text = (
            "---\n"
            'description: "Test"\n'
            "always: true\n"
            "---\n"
            "# Body\nContent\n"
        )
        meta, body = Agent.parse_skill_frontmatter(text)
        assert meta["description"] == "Test"
        assert meta["always"] is True
        assert body.startswith("# Body")

    def test_no_frontmatter(self):
        text = "# Just markdown\nNo frontmatter here."
        meta, body = Agent.parse_skill_frontmatter(text)
        assert meta is None
        assert body == text

    def test_invalid_yaml(self):
        text = "---\n{{invalid yaml\n---\n# Body"
        meta, body = Agent.parse_skill_frontmatter(text)
        assert meta is None


class TestCheckRequirements:
    def test_no_requirements(self):
        ok, errors = Agent.check_skill_requirements({"requirements": {}})
        assert ok is True
        assert errors == []

    def test_existing_command(self):
        ok, errors = Agent.check_skill_requirements(
            {"requirements": {"commands": ["python3"]}}
        )
        assert ok is True

    def test_missing_command(self):
        ok, errors = Agent.check_skill_requirements(
            {"requirements": {"commands": ["nonexistent_cmd_xyz"]}}
        )
        assert ok is False
        assert "nonexistent_cmd_xyz" in errors[0]

    def test_missing_env(self):
        ok, errors = Agent.check_skill_requirements(
            {"requirements": {"env": ["SUPER_SECRET_KEY_XXXYYY"]}}
        )
        assert ok is False
        assert "SUPER_SECRET_KEY_XXXYYY" in errors[0]

    def test_existing_env(self):
        os.environ["TEST_SKILL_KEY_123"] = "value"
        try:
            ok, errors = Agent.check_skill_requirements(
                {"requirements": {"env": ["TEST_SKILL_KEY_123"]}}
            )
            assert ok is True
        finally:
            del os.environ["TEST_SKILL_KEY_123"]


class TestLoadSkills:
    def test_loads_allowed_skill(self, agent_with_skills):
        prompt = agent_with_skills._load_skills()
        assert "Allowed Skill" in prompt
        assert "Тело скилла" in prompt

    def test_loads_always_on(self, agent_with_skills):
        prompt = agent_with_skills._load_skills()
        assert "Always On" in prompt

    def test_skips_missing_env(self, agent_with_skills):
        prompt = agent_with_skills._load_skills()
        assert "Missing Env Skill" not in prompt

    def test_skips_not_listed(self, agent_with_skills):
        prompt = agent_with_skills._load_skills()
        assert "Not Listed" not in prompt

    def test_strips_frontmatter(self, agent_with_skills):
        prompt = agent_with_skills._load_skills()
        # Frontmatter не должен попасть в system prompt
        assert "description:" not in prompt
        assert "requirements:" not in prompt

    def test_includes_legacy(self, agent_with_skills):
        # Legacy скилл без frontmatter не в списке skills — загружается как есть?
        # Нет: skill_names есть, "legacy-skill" не в списке и нет always
        # meta is None → нет фильтрации → загружается
        prompt = agent_with_skills._load_skills()
        assert "Legacy" in prompt


class TestMatchSkillTriggers:
    """Unit-тесты для match_skill_triggers (локальный pattern-match)."""

    def test_no_triggers_returns_false(self):
        assert Agent.match_skill_triggers("любое сообщение", {}) is False
        assert Agent.match_skill_triggers("любое", {"triggers": None}) is False
        assert Agent.match_skill_triggers("любое", {"triggers": {}}) is False

    def test_keyword_exact_match(self):
        meta = {"triggers": {"keywords": ["баг"]}}
        assert Agent.match_skill_triggers("у меня баг в коде", meta) is True

    def test_keyword_case_insensitive(self):
        meta = {"triggers": {"keywords": ["Error"]}}
        assert Agent.match_skill_triggers("получил ERROR в логах", meta) is True

    def test_keyword_substring_match(self):
        # "баг" должен найтись в "багов"
        meta = {"triggers": {"keywords": ["баг"]}}
        assert Agent.match_skill_triggers("исправил кучу багов", meta) is True

    def test_no_match(self):
        meta = {"triggers": {"keywords": ["баг", "ошибка"]}}
        assert Agent.match_skill_triggers("посмотри документ", meta) is False

    def test_file_extension_match(self):
        meta = {"triggers": {"file_extensions": [".pdf", ".docx"]}}
        assert Agent.match_skill_triggers("вот файл contract.pdf", meta) is True
        assert Agent.match_skill_triggers("вот файл report.docx", meta) is True

    def test_file_extension_no_match(self):
        meta = {"triggers": {"file_extensions": [".pdf"]}}
        assert Agent.match_skill_triggers("вот файл report.txt", meta) is False

    def test_multiple_keywords_any_match(self):
        meta = {"triggers": {"keywords": ["a", "b", "c"]}}
        assert Agent.match_skill_triggers("text with b inside", meta) is True

    def test_empty_keywords_list(self):
        meta = {"triggers": {"keywords": []}}
        assert Agent.match_skill_triggers("anything", meta) is False


class TestProgressiveDisclosure:
    """Интеграционные тесты progressive-режима _load_skills."""

    @pytest.fixture
    def progressive_agent(self, tmp_path):
        agent_dir = tmp_path / "agents" / "test"
        agent_dir.mkdir(parents=True)
        (agent_dir / "memory").mkdir()

        config = {
            "name": "test",
            "bot_token": "123:ABC",
            "skills": ["debugging", "document-analysis", "always-skill"],
            "memory_path": str(agent_dir / "memory"),
        }
        yaml_path = agent_dir / "agent.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(config, f)

        skills_dir = agent_dir / "skills"
        skills_dir.mkdir()

        # Скилл с триггером на "баг"
        (skills_dir / "debugging.md").write_text(
            "---\n"
            "name: debugging\n"
            'description: "Debug skill"\n'
            "triggers:\n"
            "  keywords: ['баг', 'error']\n"
            "always: false\n"
            "---\n"
            "# Debug Body\nПолная инструкция дебага.\n"
        )

        # Скилл с триггером на расширение
        (skills_dir / "document-analysis.md").write_text(
            "---\n"
            "name: document-analysis\n"
            'description: "Analyze documents"\n'
            "triggers:\n"
            "  file_extensions: ['.pdf']\n"
            "always: false\n"
            "---\n"
            "# Doc Body\nПолная инструкция анализа.\n"
        )

        # Always-on скилл — всегда полное тело
        (skills_dir / "always-skill.md").write_text(
            "---\n"
            "name: always-skill\n"
            'description: "Always loaded"\n'
            "always: true\n"
            "---\n"
            "# Always Body\nВсегда активен.\n"
        )

        return Agent(str(yaml_path))

    def test_legacy_mode_loads_all_bodies(self, progressive_agent):
        # Без user_message — полное тело всех скиллов
        prompt = progressive_agent._load_skills()
        assert "Debug Body" in prompt
        assert "Doc Body" in prompt
        assert "Always Body" in prompt

    def test_progressive_mode_metadata_only_when_no_match(self, progressive_agent):
        # Сообщение не совпадает ни с одним триггером
        prompt = progressive_agent._load_skills(user_message="привет, как дела")
        # Always-скилл всегда в полном виде
        assert "Always Body" in prompt
        # Остальные — только в каталоге, без полного тела
        assert "Debug Body" not in prompt
        assert "Doc Body" not in prompt
        # Каталог должен содержать описания
        assert "Каталог" in prompt
        assert "debugging" in prompt
        assert "document-analysis" in prompt

    def test_progressive_mode_activates_by_keyword(self, progressive_agent):
        prompt = progressive_agent._load_skills(user_message="у меня баг в коде")
        # debugging активирован
        assert "Debug Body" in prompt
        # document-analysis не активирован
        assert "Doc Body" not in prompt
        assert "Always Body" in prompt

    def test_progressive_mode_activates_by_file_extension(self, progressive_agent):
        prompt = progressive_agent._load_skills(
            user_message="посмотри contract.pdf"
        )
        assert "Doc Body" in prompt
        assert "Debug Body" not in prompt
        assert "Always Body" in prompt

    def test_progressive_mode_multiple_activations(self, progressive_agent):
        prompt = progressive_agent._load_skills(
            user_message="в этом файле report.pdf какой-то баг"
        )
        # Оба активированы
        assert "Debug Body" in prompt
        assert "Doc Body" in prompt
        assert "Always Body" in prompt

    def test_progressive_mode_activation_is_session_scoped(self, progressive_agent):
        # Первый вызов с активацией
        progressive_agent._load_skills(user_message="у меня баг")
        # Второй вызов БЕЗ триггера — прошлая активация не должна сохраниться
        prompt = progressive_agent._load_skills(user_message="привет")
        assert "Debug Body" not in prompt


class TestCheckMemoryRequirements:
    """Unit-тесты для check_skill_memory_requirements (мягкая проверка)."""

    def test_no_requirements(self, tmp_path):
        missing = Agent.check_skill_memory_requirements({}, str(tmp_path))
        assert missing == []

    def test_empty_list(self, tmp_path):
        missing = Agent.check_skill_memory_requirements(
            {"requires_memory": []}, str(tmp_path)
        )
        assert missing == []

    def test_none_value(self, tmp_path):
        missing = Agent.check_skill_memory_requirements(
            {"requires_memory": None}, str(tmp_path)
        )
        assert missing == []

    def test_all_files_present(self, tmp_path):
        (tmp_path / "wiki").mkdir()
        (tmp_path / "wiki" / "profile.md").write_text("test")
        missing = Agent.check_skill_memory_requirements(
            {"requires_memory": ["wiki/profile.md"]}, str(tmp_path)
        )
        assert missing == []

    def test_missing_file_reported(self, tmp_path):
        missing = Agent.check_skill_memory_requirements(
            {"requires_memory": ["wiki/concepts/tasks.md"]}, str(tmp_path)
        )
        assert missing == ["wiki/concepts/tasks.md"]

    def test_partial_missing(self, tmp_path):
        (tmp_path / "a.md").write_text("here")
        missing = Agent.check_skill_memory_requirements(
            {"requires_memory": ["a.md", "b.md", "c.md"]}, str(tmp_path)
        )
        assert missing == ["b.md", "c.md"]

    def test_soft_check_does_not_block_load(self, tmp_path):
        """Скилл с отсутствующим файлом памяти должен всё равно загружаться."""
        agent_dir = tmp_path / "agents" / "test"
        agent_dir.mkdir(parents=True)
        (agent_dir / "memory").mkdir()  # пустая папка, файла нет

        config = {
            "name": "test",
            "bot_token": "123:ABC",
            "skills": ["needs-memory"],
            "memory_path": str(agent_dir / "memory"),
        }
        yaml_path = agent_dir / "agent.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(config, f)

        skills_dir = agent_dir / "skills"
        skills_dir.mkdir()
        (skills_dir / "needs-memory.md").write_text(
            "---\n"
            'description: "Needs memory"\n'
            'requires_memory: ["wiki/profile.md"]\n'
            "always: false\n"
            "---\n"
            "# Needs Memory\nТело скилла.\n"
        )

        agent = Agent(str(yaml_path))
        prompt = agent._load_skills()
        # Скилл должен быть в system prompt несмотря на отсутствующий файл
        assert "Needs Memory" in prompt
        assert "Тело скилла" in prompt


class TestAgentSkillsIoFormat:
    """
    Контракт-тесты для agentskills.io-совместимого frontmatter.
    Проверяем что все реальные скиллы в agents/*/skills/ соответствуют формату.
    """

    REQUIRED_FIELDS = ["name", "version", "description", "license", "when_to_use", "tags", "requires_memory", "triggers"]

    @pytest.fixture
    def all_skill_files(self):
        root = Path(__file__).parent.parent / "agents"
        return sorted(root.glob("*/skills/*.md"))

    def test_all_skills_have_frontmatter(self, all_skill_files):
        assert len(all_skill_files) > 0, "Не найдено ни одного скилла"
        for skill_file in all_skill_files:
            text = skill_file.read_text(encoding="utf-8")
            meta, _ = Agent.parse_skill_frontmatter(text)
            assert meta is not None, f"{skill_file.name}: нет frontmatter"

    def test_all_skills_have_required_fields(self, all_skill_files):
        for skill_file in all_skill_files:
            text = skill_file.read_text(encoding="utf-8")
            meta, _ = Agent.parse_skill_frontmatter(text)
            for field in self.REQUIRED_FIELDS:
                assert field in meta, f"{skill_file.name}: отсутствует поле '{field}'"

    def test_name_matches_filename(self, all_skill_files):
        for skill_file in all_skill_files:
            text = skill_file.read_text(encoding="utf-8")
            meta, _ = Agent.parse_skill_frontmatter(text)
            assert meta["name"] == skill_file.stem, (
                f"{skill_file.name}: name='{meta['name']}' не совпадает с именем файла"
            )

    def test_version_is_semver(self, all_skill_files):
        import re
        semver = re.compile(r"^\d+\.\d+\.\d+$")
        for skill_file in all_skill_files:
            text = skill_file.read_text(encoding="utf-8")
            meta, _ = Agent.parse_skill_frontmatter(text)
            assert semver.match(str(meta["version"])), (
                f"{skill_file.name}: version='{meta['version']}' не semver"
            )

    def test_tags_is_list(self, all_skill_files):
        for skill_file in all_skill_files:
            text = skill_file.read_text(encoding="utf-8")
            meta, _ = Agent.parse_skill_frontmatter(text)
            assert isinstance(meta["tags"], list), f"{skill_file.name}: tags не список"
            assert len(meta["tags"]) > 0, f"{skill_file.name}: tags пустой"

    def test_requires_memory_is_list(self, all_skill_files):
        for skill_file in all_skill_files:
            text = skill_file.read_text(encoding="utf-8")
            meta, _ = Agent.parse_skill_frontmatter(text)
            assert isinstance(meta["requires_memory"], list), (
                f"{skill_file.name}: requires_memory не список"
            )

    def test_when_to_use_is_string(self, all_skill_files):
        for skill_file in all_skill_files:
            text = skill_file.read_text(encoding="utf-8")
            meta, _ = Agent.parse_skill_frontmatter(text)
            assert isinstance(meta["when_to_use"], str), (
                f"{skill_file.name}: when_to_use не строка"
            )
            assert len(meta["when_to_use"]) > 10, (
                f"{skill_file.name}: when_to_use слишком короткий"
            )

    def test_triggers_has_keywords_or_extensions(self, all_skill_files):
        for skill_file in all_skill_files:
            text = skill_file.read_text(encoding="utf-8")
            meta, _ = Agent.parse_skill_frontmatter(text)
            triggers = meta["triggers"]
            assert isinstance(triggers, dict), (
                f"{skill_file.name}: triggers не dict"
            )
            keywords = triggers.get("keywords") or []
            extensions = triggers.get("file_extensions") or []
            assert len(keywords) + len(extensions) > 0, (
                f"{skill_file.name}: triggers пустые (нет ни keywords ни "
                f"file_extensions) — progressive disclosure не сможет "
                f"активировать скилл"
            )
