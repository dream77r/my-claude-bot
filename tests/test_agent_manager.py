"""Тесты AgentManager — создание агентов и интеграция с templates/souls/."""

from pathlib import Path

import pytest

from src.agent_manager import AgentManager


@pytest.fixture
def root(tmp_path):
    """Минимальный root репо: agents/, templates/souls/, .env."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "templates" / "souls").mkdir(parents=True)
    (tmp_path / ".env").write_text("FOUNDER_TELEGRAM_ID=42\n")
    return tmp_path


def test_create_uses_hardcoded_template_by_default(root):
    """Без soul_template и soul_md — встроенный SOUL_MD_TEMPLATE."""
    mgr = AgentManager(root)
    agent_dir = mgr.create_agent(
        name="advisor",
        display_name="Adviser",
        bot_token="1:abc",
        description="Простой советник",
    )
    soul = (agent_dir / "SOUL.md").read_text(encoding="utf-8")
    assert "SOUL: Adviser" in soul
    assert "Простой советник" in soul


def test_create_uses_soul_template_when_present(root):
    """soul_template='team' читает templates/souls/team.md."""
    template_text = "# SOUL: Team\n\nCustom team prompt with выборочные ответы.\n"
    (root / "templates" / "souls" / "team.md").write_text(
        template_text, encoding="utf-8"
    )
    mgr = AgentManager(root)
    agent_dir = mgr.create_agent(
        name="myteam",
        display_name="My Team",
        bot_token="2:def",
        description="Командный бот",
        soul_template="team",
    )
    soul = (agent_dir / "SOUL.md").read_text(encoding="utf-8")
    assert soul == template_text


def test_create_falls_back_when_template_missing(root):
    """soul_template='nonexistent' → fallback на SOUL_MD_TEMPLATE без ошибки."""
    mgr = AgentManager(root)
    agent_dir = mgr.create_agent(
        name="ghost",
        display_name="Ghost",
        bot_token="3:ghi",
        description="Призрак",
        soul_template="nonexistent",
    )
    soul = (agent_dir / "SOUL.md").read_text(encoding="utf-8")
    assert "SOUL: Ghost" in soul  # fallback hardcoded template
    assert "Призрак" in soul


def test_explicit_soul_md_wins_over_template(root):
    """Явный soul_md приоритетнее любого soul_template."""
    (root / "templates" / "souls" / "team.md").write_text(
        "# Template Team\n", encoding="utf-8"
    )
    mgr = AgentManager(root)
    agent_dir = mgr.create_agent(
        name="custom",
        display_name="Custom",
        bot_token="4:jkl",
        description="С кастомной душой",
        soul_md="# Custom SOUL Override\n",
        soul_template="team",
    )
    soul = (agent_dir / "SOUL.md").read_text(encoding="utf-8")
    assert soul == "# Custom SOUL Override\n"
    assert "Template Team" not in soul


def test_template_has_null_backend_by_default(root):
    """Без backend в yaml пишется backend: null (следовать глобальному выбору)."""
    import yaml

    mgr = AgentManager(root)
    agent_dir = mgr.create_agent(
        name="follower",
        display_name="Follower",
        bot_token="5:mno",
        description="Без своего бэкенда",
    )
    config = yaml.safe_load((agent_dir / "agent.yaml").read_text(encoding="utf-8"))
    assert config["backend"] is None
    assert config["backend_model"] is None


def test_create_agent_with_backend(root):
    """backend='codex' попадает в yaml как явный id."""
    import yaml

    mgr = AgentManager(root)
    agent_dir = mgr.create_agent(
        name="backender",
        display_name="Backender",
        bot_token="6:pqr",
        description="На codex",
        backend="codex",
    )
    config = yaml.safe_load((agent_dir / "agent.yaml").read_text(encoding="utf-8"))
    assert config["backend"] == "codex"


def test_create_agent_rejects_unknown_backend(root):
    """Неизвестный бэкенд — ValueError на этапе валидации."""
    mgr = AgentManager(root)
    with pytest.raises(ValueError, match="Неизвестный бэкенд"):
        mgr.create_agent(
            name="bogus",
            display_name="Bogus",
            bot_token="7:stu",
            description="Сломанный бэкенд",
            backend="not-a-backend",
        )


def _make_validated_agent(root, name: str, yaml_extra: str) -> Path:
    """Минимальная структура агента для validate_agent."""
    agent_dir = root / "agents" / name
    (agent_dir / "memory").mkdir(parents=True)
    (agent_dir / "SOUL.md").write_text("# SOUL\n", encoding="utf-8")
    (agent_dir / "agent.yaml").write_text(
        f'name: "{name}"\n'
        f'bot_token: "${{{name.upper()}_BOT_TOKEN}}"\n'
        f"{yaml_extra}",
        encoding="utf-8",
    )
    return agent_dir


def test_validate_agent_unknown_backend_is_error(root):
    mgr = AgentManager(root)
    agent_dir = _make_validated_agent(root, "badbackend", "backend: nonexistent\n")
    ok, errors = mgr.validate_agent(agent_dir)
    assert not ok
    assert any("Неизвестный бэкенд" in e for e in errors)


def test_validate_agent_unknown_model_is_warning_not_error(root, caplog):
    """Модель вне списка бэкенда — warning в лог, агент валиден
    (CLI-бэкенды принимают произвольные id моделей)."""
    import logging

    mgr = AgentManager(root)
    agent_dir = _make_validated_agent(
        root,
        "freemodel",
        'backend: codex\nclaude_model: "my-custom-model"\n',
    )
    with caplog.at_level(logging.WARNING, logger="src.agent_manager"):
        ok, errors = mgr.validate_agent(agent_dir)
    assert ok, errors
    assert any("my-custom-model" in r.message for r in caplog.records)


def test_validate_agent_empty_model_skips_check(root):
    """Пустая строка модели — без проверки и без warning."""
    mgr = AgentManager(root)
    agent_dir = _make_validated_agent(
        root,
        "emptymodel",
        'backend: codex\nclaude_model: ""\n',
    )
    ok, errors = mgr.validate_agent(agent_dir)
    assert ok, errors


def test_validate_agent_known_backend_known_model_ok(root):
    mgr = AgentManager(root)
    agent_dir = _make_validated_agent(
        root,
        "goodagent",
        'backend: codex\nclaude_model: "gpt-5.1"\n',
    )
    ok, errors = mgr.validate_agent(agent_dir)
    assert ok, errors
