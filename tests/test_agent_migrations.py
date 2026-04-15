"""Тесты runtime-миграций агента."""

from pathlib import Path

import pytest

from src.agent_migrations import auto_register_builtin_skills


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agents" / "me"
    (d / "skills").mkdir(parents=True)
    return d


def test_adds_builtin_when_file_exists(agent_dir):
    (agent_dir / "skills" / "wiki-search.md").write_text("---\nname: wiki-search\n---\n")
    result = auto_register_builtin_skills(
        str(agent_dir), "me", ["document-analysis"]
    )
    assert "document-analysis" in result
    assert "wiki-search" in result


def test_skips_when_file_absent(agent_dir):
    result = auto_register_builtin_skills(
        str(agent_dir), "me", ["document-analysis"]
    )
    assert result == ["document-analysis"]


def test_idempotent(agent_dir):
    (agent_dir / "skills" / "wiki-search.md").write_text("x")
    first = auto_register_builtin_skills(str(agent_dir), "me", ["x"])
    second = auto_register_builtin_skills(str(agent_dir), "me", first)
    assert first == second
    assert second.count("wiki-search") == 1


def test_does_not_duplicate_if_already_present(agent_dir):
    (agent_dir / "skills" / "wiki-search.md").write_text("x")
    result = auto_register_builtin_skills(
        str(agent_dir), "me", ["wiki-search", "other"]
    )
    assert result.count("wiki-search") == 1


def test_does_not_mutate_input(agent_dir):
    (agent_dir / "skills" / "wiki-search.md").write_text("x")
    original = ["a", "b"]
    result = auto_register_builtin_skills(str(agent_dir), "me", original)
    assert original == ["a", "b"]
    assert "wiki-search" in result


def test_handles_missing_skills_dir(tmp_path):
    result = auto_register_builtin_skills(
        str(tmp_path / "no_such_agent"), "x", ["foo"]
    )
    assert result == ["foo"]
