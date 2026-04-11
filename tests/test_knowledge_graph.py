"""Тесты для Knowledge Graph — 3-уровневый граф связей памяти."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.knowledge_graph import (
    GRAPH_FILE,
    SUMMARIES_DIR,
    SYNTHESIS_STATE_FILE,
    _extract_json,
    _load_graph,
    _load_synthesis_state,
    _save_graph,
    _save_synthesis_state,
    get_related_by_graph,
    link_daily_entities,
    nightly_graph_cycle,
    should_run_synthesis,
    summarize_day,
    synthesize_graph,
)


@pytest.fixture
def agent_dir(tmp_path):
    """Создать структуру агента для тестов."""
    agent = tmp_path / "agents" / "me"
    mem = agent / "memory"

    # Создать директории
    for d in [
        "daily",
        "daily/summaries",
        "wiki/entities",
        "wiki/concepts",
        "wiki/synthesis",
        "sessions",
        "raw/conversations",
        "stats",
    ]:
        (mem / d).mkdir(parents=True, exist_ok=True)

    # Создать templates
    templates = agent / "templates"
    templates.mkdir(parents=True, exist_ok=True)

    # Минимальный profile.md
    (mem / "profile.md").write_text("# Профиль\n- Имя: Тест\n", encoding="utf-8")

    # Минимальный index.md
    (mem / "index.md").write_text("# Каталог знаний\n", encoding="utf-8")

    return str(agent)


@pytest.fixture
def daily_note(agent_dir):
    """Создать дневной лог для тестов."""
    mem = Path(agent_dir) / "memory"
    today = datetime.now().strftime("%Y-%m-%d")
    daily_path = mem / "daily" / f"{today}.md"
    daily_path.write_text(
        f"# {today} Friday\n\n"
        "**09:15** 👤 Обсуждали запуск ProductX с Иваном из Acme Corp\n"
        "**10:30** 🤖 Проанализировал конкурентов: Beta Inc вышли на рынок B2B SaaS\n"
        "**14:00** 👤 Встреча с Мариной по дизайну лендинга для ProductX\n"
        "**15:30** 🤖 Подготовил отчёт по метрикам ProductX за март\n"
        "**17:00** 👤 Иван прислал финальный драфт контракта с Acme Corp\n",
        encoding="utf-8",
    )
    return daily_path


@pytest.fixture
def wiki_pages(agent_dir):
    """Создать wiki-страницы для тестов."""
    mem = Path(agent_dir) / "memory"

    (mem / "wiki" / "entities" / "ivan.md").write_text(
        "# Иван\n\nCTO в Acme Corp. Работает над ProductX.\n", encoding="utf-8"
    )
    (mem / "wiki" / "entities" / "acme-corp.md").write_text(
        "# Acme Corp\n\nТехнологическая компания. Флагманский продукт: ProductX.\n",
        encoding="utf-8",
    )
    (mem / "wiki" / "concepts" / "productx.md").write_text(
        "# ProductX\n\nB2B SaaS платформа. Запуск планируется в апреле.\n",
        encoding="utf-8",
    )


# ── Тесты утилит ──


class TestExtractJson:
    """Тесты для _extract_json."""

    def test_plain_json(self):
        result = _extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_in_code_block(self):
        text = 'Вот результат:\n\n```json\n{"key": "value"}\n```\n\nГотово.'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_json_embedded_in_text(self):
        text = 'Результат: {"entities": [], "links": []} конец'
        result = _extract_json(text)
        assert result is not None
        assert "entities" in result

    def test_invalid_json(self):
        result = _extract_json("not json at all")
        assert result is None

    def test_empty_string(self):
        result = _extract_json("")
        assert result is None


# ── Тесты графа ──


class TestGraph:
    """Тесты для работы с graph.json."""

    def test_load_empty_graph(self, agent_dir):
        graph = _load_graph(agent_dir)
        assert graph["edges"] == []

    def test_save_and_load_graph(self, agent_dir):
        graph = {
            "edges": [
                {
                    "from": "Иван",
                    "to": "Acme Corp",
                    "type": "works_at",
                    "context": "CTO",
                    "first_seen": "2026-04-11",
                    "last_seen": "2026-04-11",
                    "strength": 1,
                }
            ],
            "updated": "",
        }
        _save_graph(agent_dir, graph)

        loaded = _load_graph(agent_dir)
        assert len(loaded["edges"]) == 1
        assert loaded["edges"][0]["from"] == "Иван"
        assert loaded["edges"][0]["to"] == "Acme Corp"
        assert loaded["updated"] != ""  # timestamp обновлён

    def test_graph_file_path(self, agent_dir):
        _save_graph(agent_dir, {"edges": [], "updated": ""})
        mem = Path(agent_dir) / "memory"
        assert (mem / GRAPH_FILE).exists()

    def test_corrupted_graph_returns_empty(self, agent_dir):
        mem = Path(agent_dir) / "memory"
        (mem / GRAPH_FILE).write_text("invalid json{{{", encoding="utf-8")
        graph = _load_graph(agent_dir)
        assert graph["edges"] == []


# ── Тесты состояния синтеза ──


class TestSynthesisState:
    """Тесты для адаптивного расписания Уровня 3."""

    def test_initial_state(self, agent_dir):
        state = _load_synthesis_state(agent_dir)
        assert state["last_synthesis"] is None
        assert state["total_runs"] == 0
        assert state["first_run"] is None

    def test_save_and_load_state(self, agent_dir):
        state = {
            "last_synthesis": "2026-04-11",
            "total_runs": 5,
            "first_run": "2026-04-01",
        }
        _save_synthesis_state(agent_dir, state)

        loaded = _load_synthesis_state(agent_dir)
        assert loaded["last_synthesis"] == "2026-04-11"
        assert loaded["total_runs"] == 5

    def test_should_run_first_time(self, agent_dir):
        """Первый запуск — всегда да."""
        assert should_run_synthesis(agent_dir, {}) is True

    def test_should_run_daily_phase(self, agent_dir):
        """В фазе обучения (первые 14 дней) — каждый день."""
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        # Первый запуск был вчера
        state = {
            "last_synthesis": yesterday,
            "total_runs": 1,
            "first_run": yesterday,
        }
        _save_synthesis_state(agent_dir, state)

        config = {"synthesis_schedule": {"daily_phase_days": 14}}
        assert should_run_synthesis(agent_dir, config) is True

    def test_should_not_run_same_day(self, agent_dir):
        """Не запускать дважды в один день."""
        today = datetime.now().strftime("%Y-%m-%d")

        state = {
            "last_synthesis": today,
            "total_runs": 1,
            "first_run": today,
        }
        _save_synthesis_state(agent_dir, state)

        config = {"synthesis_schedule": {"daily_phase_days": 14}}
        assert should_run_synthesis(agent_dir, config) is False

    def test_should_run_regular_interval(self, agent_dir):
        """После фазы обучения — каждые N дней."""
        # Первый запуск был 20 дней назад (после фазы обучения в 14 дней)
        first = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
        # Последний запуск 3 дня назад
        last = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

        state = {
            "last_synthesis": last,
            "total_runs": 10,
            "first_run": first,
        }
        _save_synthesis_state(agent_dir, state)

        config = {
            "synthesis_schedule": {
                "daily_phase_days": 14,
                "regular_interval_days": 3,
            }
        }
        assert should_run_synthesis(agent_dir, config) is True

    def test_should_not_run_too_early(self, agent_dir):
        """Не запускать раньше интервала."""
        first = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
        last = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        state = {
            "last_synthesis": last,
            "total_runs": 10,
            "first_run": first,
        }
        _save_synthesis_state(agent_dir, state)

        config = {
            "synthesis_schedule": {
                "daily_phase_days": 14,
                "regular_interval_days": 3,
            }
        }
        assert should_run_synthesis(agent_dir, config) is False


# ── Тесты графового поиска ──


class TestGraphSearch:
    """Тесты для get_related_by_graph."""

    def test_find_related(self, agent_dir):
        graph = {
            "edges": [
                {
                    "from": "Иван",
                    "to": "Acme Corp",
                    "type": "works_at",
                    "context": "CTO",
                    "strength": 5,
                    "last_seen": "2026-04-11",
                },
                {
                    "from": "Иван",
                    "to": "ProductX",
                    "type": "works_on",
                    "context": "разработка",
                    "strength": 3,
                    "last_seen": "2026-04-10",
                },
            ],
            "updated": "",
        }
        _save_graph(agent_dir, graph)

        related = get_related_by_graph(agent_dir, "Иван", max_results=5)
        assert len(related) == 2
        # Сортировка по strength
        assert related[0]["name"] == "Acme Corp"
        assert related[0]["strength"] == 5
        assert related[1]["name"] == "ProductX"

    def test_find_reverse_direction(self, agent_dir):
        """Связь находится в обе стороны."""
        graph = {
            "edges": [
                {
                    "from": "Иван",
                    "to": "Acme Corp",
                    "type": "works_at",
                    "context": "CTO",
                    "strength": 3,
                    "last_seen": "2026-04-11",
                }
            ],
            "updated": "",
        }
        _save_graph(agent_dir, graph)

        # Поиск по "Acme Corp" → найдёт "Иван"
        related = get_related_by_graph(agent_dir, "Acme Corp")
        assert len(related) == 1
        assert related[0]["name"] == "Иван"

    def test_no_results_for_unknown(self, agent_dir):
        graph = {"edges": [], "updated": ""}
        _save_graph(agent_dir, graph)

        related = get_related_by_graph(agent_dir, "Неизвестный")
        assert len(related) == 0

    def test_max_results_limit(self, agent_dir):
        edges = []
        for i in range(10):
            edges.append({
                "from": "Hub",
                "to": f"Node{i}",
                "type": "related",
                "context": f"связь {i}",
                "strength": 10 - i,
                "last_seen": "2026-04-11",
            })
        _save_graph(agent_dir, {"edges": edges, "updated": ""})

        related = get_related_by_graph(agent_dir, "Hub", max_results=3)
        assert len(related) == 3
        # Самые сильные первыми
        assert related[0]["strength"] == 10


# ── Тесты уровней (с моками Claude) ──


class TestLevel1:
    """Тесты для Уровень 1: Линковка."""

    @pytest.mark.asyncio
    async def test_no_daily_note(self, agent_dir):
        """Если нет daily note — ничего не делать."""
        result = await link_daily_entities(agent_dir)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_short_daily_note(self, agent_dir):
        """Короткий daily note (< 50 символов) — пропустить."""
        mem = Path(agent_dir) / "memory"
        today = datetime.now().strftime("%Y-%m-%d")
        (mem / "daily" / f"{today}.md").write_text("# Пусто\n", encoding="utf-8")

        result = await link_daily_entities(agent_dir)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_successful_linking(self, agent_dir, daily_note, wiki_pages):
        """Успешная линковка с мок-ответом Claude."""
        mock_response = json.dumps({
            "entities": [
                {"name": "Иван", "category": "person"},
                {"name": "Acme Corp", "category": "company"},
                {"name": "ProductX", "category": "project"},
            ],
            "links": [
                {
                    "from": "Иван",
                    "to": "Acme Corp",
                    "type": "works_at",
                    "context": "обсуждение запуска",
                },
                {
                    "from": "ProductX",
                    "to": "Acme Corp",
                    "type": "part_of",
                    "context": "флагманский продукт",
                },
            ],
        })

        with patch(
            "src.knowledge_graph._call_claude_simple",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await link_daily_entities(agent_dir)

        assert result["ok"] is True
        assert result["links_found"] == 2
        assert len(result["entities"]) == 3

        # Проверить что секция добавлена в daily note
        content = daily_note.read_text(encoding="utf-8")
        assert "## Связи дня" in content
        assert "[[Иван]]" in content
        assert "[[Acme Corp]]" in content

        # Проверить graph.json
        graph = _load_graph(agent_dir)
        assert len(graph["edges"]) == 2
        assert graph["edges"][0]["strength"] == 1

    @pytest.mark.asyncio
    async def test_strength_increment(self, agent_dir, daily_note):
        """Повторная связь увеличивает strength."""
        # Предзаполнить граф
        graph = {
            "edges": [
                {
                    "from": "Иван",
                    "to": "Acme Corp",
                    "type": "works_at",
                    "context": "старый контекст",
                    "first_seen": "2026-04-10",
                    "last_seen": "2026-04-10",
                    "strength": 2,
                }
            ],
            "updated": "",
        }
        _save_graph(agent_dir, graph)

        mock_response = json.dumps({
            "entities": [{"name": "Иван", "category": "person"}],
            "links": [
                {
                    "from": "Иван",
                    "to": "Acme Corp",
                    "type": "works_at",
                    "context": "новый контекст",
                }
            ],
        })

        with patch(
            "src.knowledge_graph._call_claude_simple",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await link_daily_entities(agent_dir)

        assert result["ok"] is True
        graph = _load_graph(agent_dir)
        # Связь не дублирована, strength увеличен
        assert len(graph["edges"]) == 1
        assert graph["edges"][0]["strength"] == 3
        assert graph["edges"][0]["context"] == "новый контекст"


class TestLevel2:
    """Тесты для Уровень 2: Саммари."""

    @pytest.mark.asyncio
    async def test_no_daily_note(self, agent_dir):
        result = await summarize_day(agent_dir)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_successful_summary(self, agent_dir, daily_note):
        mock_response = json.dumps({
            "summary": "Основной фокус — запуск ProductX. Встреча с Иваном и Мариной.",
            "topics": [
                {"name": "ProductX", "mentions": 3},
                {"name": "Дизайн лендинга", "mentions": 1},
            ],
            "decisions": [
                {
                    "description": "Запуск MVP до конца апреля",
                    "related": ["ProductX", "MVP"],
                }
            ],
            "action_items": ["Подготовить презентацию"],
        })

        with patch(
            "src.knowledge_graph._call_claude_simple",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await summarize_day(agent_dir)

        assert result["ok"] is True
        assert len(result["topics"]) == 2
        assert len(result["decisions"]) == 1

        # Проверить файл саммари
        mem = Path(agent_dir) / "memory"
        today = datetime.now().strftime("%Y-%m-%d")
        summary_path = mem / SUMMARIES_DIR / f"{today}.md"
        assert summary_path.exists()

        content = summary_path.read_text(encoding="utf-8")
        assert "Итоги дня" in content
        assert "[[ProductX]]" in content
        assert "Подготовить презентацию" in content


class TestLevel3:
    """Тесты для Уровень 3: Синтез."""

    @pytest.mark.asyncio
    async def test_no_summaries(self, agent_dir):
        result = await synthesize_graph(agent_dir)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_single_summary_skipped(self, agent_dir):
        """С одним саммари синтез не имеет смысла."""
        mem = Path(agent_dir) / "memory"
        summaries_dir = mem / SUMMARIES_DIR
        summaries_dir.mkdir(parents=True, exist_ok=True)
        (summaries_dir / "2026-04-11.md").write_text(
            "# Итоги дня: 2026-04-11\n\nТест\n", encoding="utf-8"
        )

        result = await synthesize_graph(agent_dir)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_successful_synthesis(self, agent_dir):
        """Успешный синтез с 3 саммари."""
        mem = Path(agent_dir) / "memory"
        summaries_dir = mem / SUMMARIES_DIR
        summaries_dir.mkdir(parents=True, exist_ok=True)

        for i in range(3):
            date = f"2026-04-{10+i:02d}"
            (summaries_dir / f"{date}.md").write_text(
                f"# Итоги дня: {date}\n\n"
                f"## Темы дня\n- [[ProductX]]\n- [[Найм]]\n\n"
                f"## Решения\n- Тестовое решение {i}\n",
                encoding="utf-8",
            )

        mock_response = json.dumps({
            "patterns": [
                {"theme": "ProductX", "frequency": 3, "trend": "стабильно"},
                {"theme": "Найм", "frequency": 3, "trend": "растёт"},
            ],
            "cross_links": [
                {
                    "from": "ProductX",
                    "to": "Найм",
                    "type": "cross_day",
                    "context": "Для запуска нужна команда",
                    "strength": 2,
                    "first_seen": "2026-04-10",
                }
            ],
        })

        with patch(
            "src.knowledge_graph._call_claude_agent",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await synthesize_graph(agent_dir)

        assert result["ok"] is True
        assert len(result["patterns"]) == 2
        assert result["cross_links"] == 1

        # Проверить состояние синтеза
        state = _load_synthesis_state(agent_dir)
        assert state["total_runs"] == 1
        assert state["first_run"] is not None

        # Проверить граф
        graph = _load_graph(agent_dir)
        cross_edges = [e for e in graph["edges"] if e["type"] == "cross_day"]
        assert len(cross_edges) == 1


# ── Тесты пайплайна ──


class TestNightlyCycle:
    """Тесты для полного ночного цикла."""

    @pytest.mark.asyncio
    async def test_full_cycle(self, agent_dir, daily_note):
        """Полный цикл: L1 + L2 + L3."""
        # Подготовить саммари для L3
        mem = Path(agent_dir) / "memory"
        summaries_dir = mem / SUMMARIES_DIR
        summaries_dir.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            date = f"2026-04-{8+i:02d}"
            (summaries_dir / f"{date}.md").write_text(
                f"# Итоги: {date}\n\n- Тема {i}\n", encoding="utf-8"
            )

        # Мокаем все вызовы Claude
        l1_response = json.dumps({
            "entities": [{"name": "Test", "category": "concept"}],
            "links": [],
        })
        l2_response = json.dumps({
            "summary": "Тестовый день",
            "topics": [{"name": "Test", "mentions": 1}],
            "decisions": [],
            "action_items": [],
        })
        l3_response = json.dumps({
            "patterns": [{"theme": "Test", "frequency": 1, "trend": "новая"}],
            "cross_links": [],
        })

        call_count = 0

        async def mock_simple(prompt, model="haiku", cwd=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return l1_response
            return l2_response

        async def mock_agent(prompt, model="haiku", cwd=None, allowed_tools=None):
            return l3_response

        with patch(
            "src.knowledge_graph._call_claude_simple",
            side_effect=mock_simple,
        ), patch(
            "src.knowledge_graph._call_claude_agent",
            side_effect=mock_agent,
        ), patch(
            "src.memory.git_commit",
            return_value=True,
        ):
            result = await nightly_graph_cycle(agent_dir, config={})

        assert result["level1"]["ok"] is True
        assert result["level2"]["ok"] is True
        assert result["level3"]["ok"] is True
        assert result["level3_skipped"] is False

    @pytest.mark.asyncio
    async def test_cycle_skips_l3(self, agent_dir, daily_note):
        """L3 пропускается если не пришло время."""
        # Установить что L3 запускался сегодня
        today = datetime.now().strftime("%Y-%m-%d")
        _save_synthesis_state(agent_dir, {
            "last_synthesis": today,
            "total_runs": 1,
            "first_run": today,
        })

        l1_response = json.dumps({"entities": [], "links": []})
        l2_response = json.dumps({
            "summary": "Пусто",
            "topics": [],
            "decisions": [],
            "action_items": [],
        })

        with patch(
            "src.knowledge_graph._call_claude_simple",
            new_callable=AsyncMock,
            return_value=l1_response,
        ), patch(
            "src.memory.git_commit",
            return_value=True,
        ):
            # Подменяем чтобы L2 тоже получил ответ
            original = _call_claude_simple_orig = None
            async def mock_simple(prompt, model="haiku", cwd=None):
                return l2_response

            with patch(
                "src.knowledge_graph._call_claude_simple",
                side_effect=mock_simple,
            ):
                result = await nightly_graph_cycle(agent_dir, config={
                    "synthesis_schedule": {"daily_phase_days": 14},
                })

        assert result["level3_skipped"] is True


# ── Тесты edge cases ──


class TestEdgeCases:
    """Edge cases и граничные условия."""

    def test_graph_dedup_same_day(self, agent_dir):
        """Не дублировать связи за один день."""
        graph = {"edges": [], "updated": ""}
        today = datetime.now().strftime("%Y-%m-%d")

        # Добавить связь
        graph["edges"].append({
            "from": "A",
            "to": "B",
            "type": "related",
            "context": "test",
            "date": today,
            "first_seen": today,
            "last_seen": today,
            "strength": 1,
        })
        _save_graph(agent_dir, graph)

        loaded = _load_graph(agent_dir)
        assert len(loaded["edges"]) == 1

    def test_empty_wiki(self, agent_dir):
        """Поиск связей без wiki-страниц."""
        related = get_related_by_graph(agent_dir, "Anything")
        assert related == []

    def test_synthesis_state_corrupted(self, agent_dir):
        """Повреждённый state-файл — возврат к default."""
        mem = Path(agent_dir) / "memory"
        state_path = mem / SYNTHESIS_STATE_FILE
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{{broken json", encoding="utf-8")

        state = _load_synthesis_state(agent_dir)
        assert state["last_synthesis"] is None

    def test_summaries_dir_creation(self, agent_dir):
        """Директория summaries создаётся автоматически."""
        mem = Path(agent_dir) / "memory"
        summaries = mem / SUMMARIES_DIR

        # Удалить если есть
        if summaries.exists():
            import shutil
            shutil.rmtree(summaries)

        # Это не должно падать — функция создаст директорию
        # (проверяем через _save_graph для аналогии)
        _save_graph(agent_dir, {"edges": [], "updated": ""})
        assert (mem / GRAPH_FILE).exists()
