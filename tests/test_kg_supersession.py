"""Тесты supersession + confidence (Этап 4 KG_WIKI_PLAN.md)."""

from src.knowledge_graph import (
    _apply_supersession,
    _edge_id,
    _EXCLUSIVE_LINK_TYPES,
)
from src.wiki_search import bfs


def _make_edge(**kwargs) -> dict:
    e = {
        "from": kwargs.get("from_", "A"),
        "to": kwargs.get("to", "B"),
        "type": kwargs.get("type", "related"),
        "date": kwargs.get("date", "2026-04-15"),
        "first_seen": kwargs.get("date", "2026-04-15"),
        "last_seen": kwargs.get("date", "2026-04-15"),
        "strength": 1,
        "confidence": kwargs.get("confidence", 1.0),
    }
    if "supersedes" in kwargs:
        e["supersedes"] = kwargs["supersedes"]
    e["id"] = _edge_id(e)
    return e


def test_edge_id_is_stable():
    e1 = {"from": "A", "to": "B", "type": "works_at", "first_seen": "2026-04-15"}
    e2 = {"from": "A", "to": "B", "type": "works_at", "first_seen": "2026-04-15"}
    assert _edge_id(e1) == _edge_id(e2)


def test_exclusive_link_supersedes_old():
    """Иван works_at Acme → потом Иван works_at Beta — старое superseded."""
    old = _make_edge(from_="Иван", to="Acme", type="works_at", date="2026-04-10")
    graph = {"edges": [old]}
    new = _make_edge(from_="Иван", to="Beta", type="works_at", date="2026-04-15")

    _apply_supersession(graph, new)
    graph["edges"].append(new)

    assert old.get("superseded_by") == new["id"]
    assert old.get("superseded_at") == "2026-04-15"
    assert not new.get("superseded_by")


def test_non_exclusive_link_coexists():
    """Phase5 depends_on MCP + Phase5 depends_on Auth — обе валидны."""
    old = _make_edge(from_="Phase5", to="MCP", type="depends_on", date="2026-04-10")
    graph = {"edges": [old]}
    new = _make_edge(from_="Phase5", to="Auth", type="depends_on", date="2026-04-15")

    _apply_supersession(graph, new)
    graph["edges"].append(new)

    assert not old.get("superseded_by")
    assert not new.get("superseded_by")


def test_exclusive_does_not_supersede_same_target():
    """Если to не изменился — это просто повтор, не supersession."""
    old = _make_edge(from_="Иван", to="Acme", type="works_at", date="2026-04-10")
    graph = {"edges": [old]}
    new = _make_edge(from_="Иван", to="Acme", type="works_at", date="2026-04-15")

    _apply_supersession(graph, new)
    assert not old.get("superseded_by")


def test_explicit_supersedes_marks_named_entities():
    """LLM явно сказал: новая Decision отменяет старую."""
    old = _make_edge(
        from_="Choose Postgres", to="Backend", type="decided_in", date="2026-04-10"
    )
    graph = {"edges": [old]}
    new = _make_edge(
        from_="Choose MongoDB",
        to="Backend",
        type="decided_in",
        date="2026-04-15",
        supersedes="Choose Postgres",
    )
    _apply_supersession(graph, new)

    assert old.get("superseded_by") == new["id"]


def test_already_superseded_edges_skipped():
    """Старое уже superseded — не перезаписываем."""
    older = _make_edge(from_="Иван", to="Old1", type="works_at", date="2026-04-01")
    older["superseded_by"] = "marker"
    middle = _make_edge(from_="Иван", to="Old2", type="works_at", date="2026-04-05")
    graph = {"edges": [older, middle]}

    new = _make_edge(from_="Иван", to="Current", type="works_at", date="2026-04-15")
    _apply_supersession(graph, new)

    assert older["superseded_by"] == "marker"  # не перезаписано
    assert middle["superseded_by"] == new["id"]


def test_bfs_ignores_superseded_by_default():
    old = _make_edge(from_="Иван", to="Acme", type="works_at", date="2026-04-10")
    new = _make_edge(from_="Иван", to="Beta", type="works_at", date="2026-04-15")
    graph = {"edges": [old, new]}
    _apply_supersession(graph, new)

    neighbors = bfs(graph, "Иван", depth=1)
    assert "Beta" in neighbors
    assert "Acme" not in neighbors


def test_bfs_includes_superseded_when_requested():
    old = _make_edge(from_="Иван", to="Acme", type="works_at", date="2026-04-10")
    new = _make_edge(from_="Иван", to="Beta", type="works_at", date="2026-04-15")
    graph = {"edges": [old, new]}
    _apply_supersession(graph, new)

    neighbors = bfs(graph, "Иван", depth=1, include_superseded=True)
    assert "Acme" in neighbors
    assert "Beta" in neighbors


def test_exclusive_types_include_works_at():
    assert "works_at" in _EXCLUSIVE_LINK_TYPES
    assert "owns" in _EXCLUSIVE_LINK_TYPES
    # related — не exclusive
    assert "related" not in _EXCLUSIVE_LINK_TYPES
    assert "depends_on" not in _EXCLUSIVE_LINK_TYPES
