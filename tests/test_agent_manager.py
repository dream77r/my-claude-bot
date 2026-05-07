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
