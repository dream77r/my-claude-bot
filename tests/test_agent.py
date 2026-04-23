"""Тесты для agent.py."""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from src.agent import Agent
from src.memory import ensure_dirs, save_session_id


@pytest.fixture
def agent_yaml(tmp_path):
    """Создать минимальный agent.yaml для тестов."""
    agent_dir = tmp_path / "agents" / "test"
    agent_dir.mkdir(parents=True)

    config = {
        "name": "test",
        "display_name": "Тестовый агент",
        "bot_token": "123:ABC",
        "system_prompt": "Ты тестовый агент.",
        "memory_path": "./agents/test/memory/",
        "skills": [],
        "allowed_users": [12345, 67890],
        "max_context_messages": 10,
        "claude_model": "sonnet",
        "claude_flags": [
            "--allowedTools",
            "Read,Write,Glob",
            "--output-format",
            "text",
        ],
    }

    yaml_path = agent_dir / "agent.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(config, f, allow_unicode=True)

    return str(yaml_path)


@pytest.fixture
def agent(agent_yaml):
    """Создать Agent из тестового YAML."""
    return Agent(agent_yaml)


class TestAgentInit:
    def test_loads_config(self, agent):
        assert agent.name == "test"
        assert agent.display_name == "Тестовый агент"
        assert agent.bot_token == "123:ABC"
        assert agent.max_context_messages == 10

    def test_parses_allowed_users(self, agent):
        assert agent.allowed_users == [12345, 67890]

    def test_creates_memory_dirs(self, agent):
        memory_path = Path(agent.agent_dir) / "memory"
        assert memory_path.exists()


class TestExpandVars:
    def test_expands_env_vars(self, tmp_path):
        agent_dir = tmp_path / "agents" / "envtest"
        agent_dir.mkdir(parents=True)

        config = {
            "name": "envtest",
            "bot_token": "${TEST_BOT_TOKEN}",
            "allowed_users": ["${TEST_USER_ID}"],
        }

        yaml_path = agent_dir / "agent.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(config, f)

        os.environ["TEST_BOT_TOKEN"] = "real-token-123"
        os.environ["TEST_USER_ID"] = "99999"
        try:
            agent = Agent(str(yaml_path))
            assert agent.bot_token == "real-token-123"
            assert 99999 in agent.allowed_users
        finally:
            del os.environ["TEST_BOT_TOKEN"]
            del os.environ["TEST_USER_ID"]


class TestIsUserAllowed:
    def test_allowed_user(self, agent):
        assert agent.is_user_allowed(12345) is True

    def test_disallowed_user(self, agent):
        assert agent.is_user_allowed(99999) is False

    def test_empty_list_allows_all(self, tmp_path):
        agent_dir = tmp_path / "agents" / "open"
        agent_dir.mkdir(parents=True)
        config = {"name": "open", "bot_token": "123:ABC", "allowed_users": []}
        yaml_path = agent_dir / "agent.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(config, f)
        agent = Agent(str(yaml_path))
        assert agent.is_user_allowed(99999) is True


class TestBuildSystemPrompt:
    def test_includes_system_prompt(self, agent):
        prompt = agent.build_system_prompt()
        assert "тестовый агент" in prompt.lower()

    def test_includes_soul(self, agent):
        soul_path = Path(agent.agent_dir) / "SOUL.md"
        soul_path.write_text("# SOUL\nЯ — мудрый агент.")
        prompt = agent.build_system_prompt()
        assert "мудрый агент" in prompt

    def test_includes_skills(self, agent):
        skills_dir = Path(agent.agent_dir) / "skills"
        skills_dir.mkdir(exist_ok=True)
        (skills_dir / "test-skill.md").write_text("# Skill: Test\nДелай тест.")
        prompt = agent.build_system_prompt()
        assert "Делай тест" in prompt

    def test_includes_memory_context(self, agent):
        memory_path = Path(agent.agent_dir) / "memory"
        (memory_path / "profile.md").write_text("# Профиль\nФаундер стартапа")
        prompt = agent.build_system_prompt()
        assert "Фаундер стартапа" in prompt


class TestParseAllowedTools:
    def test_parses_tools(self, agent):
        tools = agent._parse_allowed_tools()
        assert tools == ["Read", "Write", "Glob"]

    def test_no_tools_flag(self, tmp_path):
        agent_dir = tmp_path / "agents" / "notools"
        agent_dir.mkdir(parents=True)
        config = {"name": "notools", "bot_token": "123:ABC", "claude_flags": []}
        yaml_path = agent_dir / "agent.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(config, f)
        agent = Agent(str(yaml_path))
        assert agent._parse_allowed_tools() is None


def _make_agent(tmp_path, *, role="worker", allowed_tools=None, sandbox=None):
    """Минимальный Agent для тестов augment-логики."""
    agent_dir = tmp_path / "agents" / "t"
    agent_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "name": "t",
        "bot_token": "1:A",
        "role": role,
        "claude_flags": (
            ["--allowedTools", ",".join(allowed_tools)]
            if allowed_tools is not None
            else []
        ),
    }
    if sandbox is not None:
        config["sandbox"] = sandbox
    yaml_path = agent_dir / "agent.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(config, f)
    return Agent(str(yaml_path))


class TestAugmentWorkerFileTools:
    """Runtime-политика: worker с sandbox.enabled получает Bash+Edit."""

    def test_worker_default_sandbox_gets_bash_and_edit(self, tmp_path):
        # sandbox не задан → default для worker = True → augment работает
        agent = _make_agent(
            tmp_path, role="worker", allowed_tools=["Read", "Write", "Glob", "Grep"]
        )
        result = agent._augment_worker_file_tools(agent._parse_allowed_tools())
        assert "Bash" in result
        assert "Edit" in result
        # оригинальные tools сохранены
        assert {"Read", "Write", "Glob", "Grep"}.issubset(set(result))

    def test_worker_already_has_bash_idempotent(self, tmp_path):
        agent = _make_agent(
            tmp_path,
            role="worker",
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        )
        result = agent._augment_worker_file_tools(agent._parse_allowed_tools())
        # Без дубликатов
        assert result.count("Bash") == 1
        assert result.count("Edit") == 1

    def test_master_not_augmented(self, tmp_path):
        agent = _make_agent(
            tmp_path,
            role="master",
            allowed_tools=["Read", "Write"],
            sandbox={"enabled": True},
        )
        result = agent._augment_worker_file_tools(agent._parse_allowed_tools())
        # Master руками управляет своими tools, runtime не вмешивается
        assert "Bash" not in result
        assert "Edit" not in result

    def test_sandbox_disabled_not_augmented(self, tmp_path):
        # Worker явно выключил sandbox (как coder) → runtime не добавляет
        # Bash за его спиной, автор конфига знал что делал.
        agent = _make_agent(
            tmp_path,
            role="worker",
            allowed_tools=["Read", "Write"],
            sandbox={"enabled": False},
        )
        result = agent._augment_worker_file_tools(agent._parse_allowed_tools())
        assert "Bash" not in result

    def test_none_tools_preserved(self, tmp_path):
        # Нет --allowedTools вообще → не наш случай, None пропускается
        agent = _make_agent(tmp_path, role="worker", allowed_tools=None)
        result = agent._augment_worker_file_tools(None)
        assert result is None
