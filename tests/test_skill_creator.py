"""Тесты для SkillCreator — динамическое создание скиллов."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from src.skill_creator import (
    create_from_suggestion,
    install_skill,
    list_skills,
    remove_skill,
    validate_skill,
)


@pytest.fixture
def agent_dir(tmp_path):
    """Создать директорию агента с минимальной структурой."""
    agent_dir = tmp_path / "agents" / "test"
    agent_dir.mkdir(parents=True)

    # agent.yaml
    config = {"name": "test", "bot_token": "123:ABC"}
    (agent_dir / "agent.yaml").write_text(
        yaml.dump(config), encoding="utf-8"
    )

    # memory/
    (agent_dir / "memory").mkdir()

    # skills/
    (agent_dir / "skills").mkdir()

    return str(agent_dir)


@pytest.fixture
def agent_with_skills(agent_dir):
    """Агент с существующими скиллами."""
    skills_dir = Path(agent_dir) / "skills"

    (skills_dir / "web-research.md").write_text(
        '---\ndescription: "Поиск в интернете"\nalways: false\n---\n'
        "# Skill: Веб-исследование\n\nИнструкции для поиска.\n",
        encoding="utf-8",
    )

    (skills_dir / "debugging.md").write_text(
        '---\ndescription: "Отладка кода"\nalways: true\n---\n'
        "# Skill: Debugging\n\nИнструкции для отладки.\n",
        encoding="utf-8",
    )

    return agent_dir


# ── list_skills ──


class TestListSkills:
    def test_empty(self, agent_dir):
        result = list_skills(agent_dir)
        assert result == []

    def test_lists_all(self, agent_with_skills):
        result = list_skills(agent_with_skills)
        assert len(result) == 2
        names = {s["name"] for s in result}
        assert names == {"web-research", "debugging"}

    def test_includes_metadata(self, agent_with_skills):
        result = list_skills(agent_with_skills)
        debugging = next(s for s in result if s["name"] == "debugging")
        assert debugging["description"] == "Отладка кода"
        assert debugging["always"] is True

    def test_no_skills_dir(self, tmp_path):
        agent_dir = tmp_path / "no-skills"
        agent_dir.mkdir()
        result = list_skills(str(agent_dir))
        assert result == []


# ── validate_skill ──


class TestValidateSkill:
    def test_valid_skill(self, agent_dir):
        content = (
            '---\ndescription: "Тестовый скилл"\n'
            "requirements:\n  commands: []\n  env: []\nalways: false\n---\n\n"
            "# Skill: Test\n\nИнструкции для теста.\n"
        )
        ok, errors = validate_skill("test-skill", content, agent_dir)
        assert ok is True
        assert errors == []

    def test_invalid_name_uppercase(self, agent_dir):
        content = '---\ndescription: "Test"\n---\n\n# Skill\nBody text here.\n'
        ok, errors = validate_skill("TestSkill", content, agent_dir)
        assert ok is False
        assert any("kebab-case" in e for e in errors)

    def test_invalid_name_spaces(self, agent_dir):
        content = '---\ndescription: "Test"\n---\n\n# Skill\nBody text here.\n'
        ok, errors = validate_skill("test skill", content, agent_dir)
        assert ok is False

    def test_empty_name(self, agent_dir):
        content = '---\ndescription: "Test"\n---\n\n# Skill\nBody text here.\n'
        ok, errors = validate_skill("", content, agent_dir)
        assert ok is False
        assert any("пустое" in e for e in errors)

    def test_duplicate_name(self, agent_with_skills):
        content = '---\ndescription: "Дубликат"\n---\n\n# Skill\nBody text here.\n'
        ok, errors = validate_skill("debugging", content, agent_with_skills)
        assert ok is False
        assert any("уже существует" in e for e in errors)

    def test_no_frontmatter(self, agent_dir):
        content = "# Just markdown\nNo frontmatter.\n"
        ok, errors = validate_skill("test-skill", content, agent_dir)
        assert ok is False
        assert any("frontmatter" in e for e in errors)

    def test_no_description(self, agent_dir):
        content = "---\nalways: false\n---\n\n# Skill\nBody text with some content here.\n"
        ok, errors = validate_skill("test-skill", content, agent_dir)
        assert ok is False
        assert any("description" in e for e in errors)

    def test_short_body(self, agent_dir):
        content = '---\ndescription: "Test"\n---\n\nShort.\n'
        ok, errors = validate_skill("test-skill", content, agent_dir)
        assert ok is False
        assert any("короткое" in e for e in errors)


# ── install_skill ──


class TestInstallSkill:
    def test_creates_file(self, agent_dir):
        content = (
            '---\ndescription: "Новый скилл"\nalways: false\n---\n\n'
            "# Skill: New\n\nИнструкции.\n"
        )
        with patch("src.skill_creator.memory") as mock_memory:
            mock_memory.git_commit.return_value = True
            path = install_skill("new-skill", content, agent_dir)

        assert path.exists()
        assert path.name == "new-skill.md"
        assert "Новый скилл" in path.read_text(encoding="utf-8")

    def test_creates_skills_dir(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        # Нет skills/ — должен создать
        content = '---\ndescription: "Test"\n---\n\n# Skill\nBody here.\n'

        with patch("src.skill_creator.memory") as mock_memory:
            mock_memory.git_commit.return_value = True
            path = install_skill("test", content, str(agent_dir))

        assert (agent_dir / "skills").exists()
        assert path.exists()

    def test_git_commit(self, agent_dir):
        content = '---\ndescription: "Test"\n---\n\n# Skill\nBody here.\n'

        with patch("src.skill_creator.memory") as mock_memory:
            mock_memory.git_commit.return_value = True
            install_skill("test", content, agent_dir)
            mock_memory.git_commit.assert_called_once_with(
                agent_dir, "SkillCreator: add skill 'test'"
            )

    def test_no_commit(self, agent_dir):
        content = '---\ndescription: "Test"\n---\n\n# Skill\nBody here.\n'

        with patch("src.skill_creator.memory") as mock_memory:
            install_skill("test", content, agent_dir, commit=False)
            mock_memory.git_commit.assert_not_called()


# ── remove_skill ──


class TestRemoveSkill:
    def test_removes_existing(self, agent_with_skills):
        with patch("src.skill_creator.memory") as mock_memory:
            mock_memory.git_commit.return_value = True
            ok = remove_skill("debugging", agent_with_skills)

        assert ok is True
        assert not (Path(agent_with_skills) / "skills" / "debugging.md").exists()

    def test_not_found(self, agent_dir):
        with patch("src.skill_creator.memory"):
            ok = remove_skill("nonexistent", agent_dir)
        assert ok is False

    def test_git_commit_on_remove(self, agent_with_skills):
        with patch("src.skill_creator.memory") as mock_memory:
            mock_memory.git_commit.return_value = True
            remove_skill("debugging", agent_with_skills)
            mock_memory.git_commit.assert_called_once_with(
                agent_with_skills, "SkillCreator: remove skill 'debugging'"
            )


# ── create_from_suggestion ──


class TestCreateFromSuggestion:
    def test_creates_from_suggestion(self, agent_dir):
        suggestion = {
            "agent_name": "coder",
            "pattern": "Когда просят сделать SQL запрос",
            "frequency": 7,
            "examples": [
                "напиши SELECT для пользователей",
                "как сджойнить таблицы",
            ],
            "suggested_skill": {
                "name": "sql-generator",
                "title": "Генератор SQL",
                "description": "Генерация SQL запросов из текстового описания",
                "capabilities": [
                    "SELECT / INSERT / UPDATE / DELETE",
                    "JOIN и подзапросы",
                ],
            },
            "confidence": "high",
        }

        with patch("src.skill_creator.memory") as mock_memory:
            mock_memory.git_commit.return_value = True
            ok, msg = create_from_suggestion(suggestion, agent_dir)

        assert ok is True
        assert "sql-generator" in msg

        # Проверить файл
        path = Path(agent_dir) / "skills" / "sql-generator.md"
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "Генератор SQL" in content
        assert "SELECT" in content
        assert "JOIN" in content

    def test_rejects_no_name(self, agent_dir):
        suggestion = {
            "suggested_skill": {"title": "No name", "description": "Test"},
        }
        with patch("src.skill_creator.memory"):
            ok, msg = create_from_suggestion(suggestion, agent_dir)
        assert ok is False

    def test_rejects_duplicate(self, agent_with_skills):
        suggestion = {
            "suggested_skill": {
                "name": "debugging",
                "title": "Duplicate",
                "description": "Already exists",
                "capabilities": [],
            },
        }
        with patch("src.skill_creator.memory"):
            ok, msg = create_from_suggestion(suggestion, agent_with_skills)
        assert ok is False
        assert "уже существует" in msg


# ── generate_skill (мокаем Claude) ──


class TestGenerateSkill:
    @pytest.mark.asyncio
    async def test_generate_success(self, agent_dir):
        """Тест генерации скилла с мок-ответом Claude."""
        from src.skill_creator import generate_skill

        mock_response = json.dumps({
            "name": "competitor-analysis",
            "skill_content": (
                '---\ndescription: "Анализ конкурентов"\n'
                "requirements:\n  commands: []\n  env: []\nalways: false\n"
                "---\n\n# Skill: Анализ конкурентов\n\n"
                "## Когда активировать\n"
                "Когда пользователь просит проанализировать конкурентов.\n\n"
                "## Инструкции\n1. Определи рынок\n2. Найди конкурентов\n"
            ),
        })

        with patch(
            "src.skill_creator._call_claude_simple",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            name, content, error = await generate_skill(
                "анализ конкурентов", agent_dir, "test", "worker"
            )

        assert error == ""
        assert name == "competitor-analysis"
        assert "Анализ конкурентов" in content

    @pytest.mark.asyncio
    async def test_generate_claude_error(self, agent_dir):
        """Тест обработки ошибки Claude."""
        from src.skill_creator import generate_skill

        with patch(
            "src.skill_creator._call_claude_simple",
            new_callable=AsyncMock,
            side_effect=Exception("API error"),
        ):
            name, content, error = await generate_skill(
                "тест", agent_dir, "test", "worker"
            )

        assert name is None
        assert content is None
        assert "API error" in error

    @pytest.mark.asyncio
    async def test_generate_bad_json(self, agent_dir):
        """Тест обработки невалидного JSON от Claude."""
        from src.skill_creator import generate_skill

        with patch(
            "src.skill_creator._call_claude_simple",
            new_callable=AsyncMock,
            return_value="This is not JSON at all",
        ):
            name, content, error = await generate_skill(
                "тест", agent_dir, "test", "worker"
            )

        assert name is None
        assert "разобрать" in error


# ── create_skill (полный цикл) ──


class TestCreateSkillFull:
    @pytest.mark.asyncio
    async def test_full_cycle(self, agent_dir):
        """Полный цикл: генерация → валидация → установка."""
        from src.skill_creator import create_skill

        mock_response = json.dumps({
            "name": "daily-report",
            "skill_content": (
                '---\ndescription: "Ежедневный отчёт"\n'
                "requirements:\n  commands: []\n  env: []\nalways: false\n"
                "---\n\n# Skill: Ежедневный отчёт\n\n"
                "## Когда активировать\nКогда просят отчёт за день.\n\n"
                "## Инструкции\n1. Собери данные\n2. Сформируй отчёт\n"
            ),
        })

        with (
            patch(
                "src.skill_creator._call_claude_simple",
                new_callable=AsyncMock,
                return_value=mock_response,
            ),
            patch("src.skill_creator.memory") as mock_memory,
        ):
            mock_memory.git_commit.return_value = True
            ok, msg = await create_skill(
                "ежедневный отчёт", agent_dir, "test", "master"
            )

        assert ok is True
        assert "daily-report" in msg

        path = Path(agent_dir) / "skills" / "daily-report.md"
        assert path.exists()

    @pytest.mark.asyncio
    async def test_rejects_invalid(self, agent_dir):
        """Отклонение скилла с невалидным именем."""
        from src.skill_creator import create_skill

        mock_response = json.dumps({
            "name": "INVALID NAME!!!",
            "skill_content": '---\ndescription: "Test"\n---\n\n# Skill\nBody.\n',
        })

        with patch(
            "src.skill_creator._call_claude_simple",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            ok, msg = await create_skill(
                "тест", agent_dir, "test", "worker"
            )

        assert ok is False
        assert "валидацию" in msg


# ── get_all_agent_dirs ──


class TestGetAllAgentDirs:
    def test_finds_agents(self, tmp_path):
        from src.skill_creator import get_all_agent_dirs

        agents_dir = tmp_path / "agents"
        for name in ["me", "coder", "team"]:
            d = agents_dir / name
            d.mkdir(parents=True)
            (d / "agent.yaml").write_text(
                yaml.dump({"name": name, "bot_token": "t"}),
                encoding="utf-8",
            )

        result = get_all_agent_dirs(str(tmp_path))
        assert set(result.keys()) == {"me", "coder", "team"}

    def test_no_agents_dir(self, tmp_path):
        from src.skill_creator import get_all_agent_dirs

        result = get_all_agent_dirs(str(tmp_path))
        assert result == {}
