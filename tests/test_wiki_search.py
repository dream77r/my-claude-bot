"""Тесты wiki_search — BM25 + BFS + цитаты (этап 2)."""

import json
from pathlib import Path

import pytest

from src.wiki_search import (
    bfs,
    quote_from_daily,
    recall,
    search,
)


@pytest.fixture
def agent_dir(tmp_path):
    """Минимальная структура агента с пустой памятью."""
    agent = tmp_path / "agents" / "me"
    mem = agent / "memory"
    for d in ["wiki/entities", "wiki/concepts", "wiki/synthesis", "daily"]:
        (mem / d).mkdir(parents=True, exist_ok=True)
    return str(agent)


@pytest.fixture
def populated_agent(agent_dir):
    """Агент с фейковой Phase5-страницей и графом."""
    mem = Path(agent_dir) / "memory"
    (mem / "wiki" / "concepts" / "Phase5.md").write_text(
        "# Phase 5\n\nБезопасность, MCP-маркетплейс и UX-улучшения.\n"
        "Зависит от Phase 4 (память) и блокирует production deploy.\n",
        encoding="utf-8",
    )
    (mem / "wiki" / "entities" / "Иван.md").write_text(
        "# Иван\n\nFounder Acme Corp, отвечает за Phase 5 со стороны заказчика.\n",
        encoding="utf-8",
    )
    (mem / "wiki" / "concepts" / "MCP.md").write_text(
        "# MCP\n\nModel Context Protocol — стандарт интеграции инструментов.\n",
        encoding="utf-8",
    )
    graph = {
        "edges": [
            {
                "from": "Phase5",
                "to": "Иван",
                "type": "owned_by",
                "first_seen": "2026-04-10",
                "last_seen": "2026-04-12",
                "strength": 2,
            },
            {
                "from": "Phase5",
                "to": "MCP",
                "type": "depends_on",
                "first_seen": "2026-04-11",
                "last_seen": "2026-04-12",
                "strength": 1,
            },
        ],
        "updated": "2026-04-12T10:00:00",
    }
    (mem / "graph.json").write_text(
        json.dumps(graph, ensure_ascii=False), encoding="utf-8"
    )
    (mem / "daily" / "2026-04-12.md").write_text(
        "# 2026-04-12 Sunday\n\n"
        "**09:15** 👤 Обсудили Phase5 с Иваном — он хочет MCP уже на этой неделе.\n"
        "**09:20** 🤖 Понял, ставлю в план.\n",
        encoding="utf-8",
    )
    return agent_dir


def test_search_finds_existing_entity(populated_agent):
    hits = search("Phase 5", populated_agent)
    assert len(hits) >= 1
    names = [h.name for h in hits]
    assert "Phase5" in names
    top = next(h for h in hits if h.name == "Phase5")
    assert top.score > 0
    assert "Безопасность" in top.snippet or "Phase 5" in top.snippet


def test_search_empty_graph_does_not_crash(agent_dir):
    hits = search("anything", agent_dir)
    assert hits == []


def test_search_unknown_entity_returns_empty(populated_agent):
    hits = search("совершенно_несуществующее_слово_xyz", populated_agent)
    assert hits == []


def test_bfs_finds_neighbors(populated_agent):
    from src.wiki_search import _load_graph
    graph = _load_graph(populated_agent)
    neighbors = bfs(graph, "Phase5", depth=1)
    assert "Иван" in neighbors
    assert "MCP" in neighbors


def test_bfs_unknown_start_returns_empty(populated_agent):
    from src.wiki_search import _load_graph
    graph = _load_graph(populated_agent)
    assert bfs(graph, "NoSuchEntity", depth=1) == []


def test_bfs_empty_graph(agent_dir):
    assert bfs({"edges": []}, "Phase5", depth=1) == []


def test_quote_from_daily_finds_mention(populated_agent):
    quote = quote_from_daily("Phase5", "2026-04-12", populated_agent)
    assert "Phase5" in quote
    assert "Иван" in quote


def test_quote_from_daily_missing_date(populated_agent):
    assert quote_from_daily("Phase5", "1999-01-01", populated_agent) == ""


def test_recall_full_pipeline(populated_agent):
    result = recall("Phase 5", populated_agent)
    assert result["query"] == "Phase 5"
    assert len(result["hits"]) >= 1
    top = result["hits"][0]
    assert top["name"] == "Phase5"
    assert "Иван" in top["neighbors"]
    assert "MCP" in top["neighbors"]
    assert top["quote_date"] == "2026-04-12"
    assert "Phase5" in top["quote"]


def test_recall_empty_graph_does_not_crash(agent_dir):
    result = recall("anything", agent_dir)
    assert result["query"] == "anything"
    assert result["hits"] == []
    assert result["extra_neighbors"] == []
