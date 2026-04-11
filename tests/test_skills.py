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
