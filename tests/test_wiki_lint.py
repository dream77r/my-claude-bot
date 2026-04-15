"""Тесты wiki_lint (Этап 5 KG_WIKI_PLAN.md)."""

import json
from pathlib import Path

import pytest

from src.wiki_lint import _name_is_blocked, lint_wiki, run_lint, write_report


@pytest.fixture
def agent_dir(tmp_path):
    a = tmp_path / "agents" / "me"
    (a / "memory" / "wiki" / "people").mkdir(parents=True)
    (a / "memory" / "wiki" / "projects").mkdir(parents=True)
    (a / "memory" / "wiki" / "synthesis").mkdir(parents=True)
    return str(a)


def _write_page(agent_dir: str, folder: str, name: str, text: str | None = None) -> None:
    p = Path(agent_dir) / "memory" / "wiki" / folder / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text or f"---\ntype: Person\n---\n\n# {name}\n", encoding="utf-8")


def _write_graph(agent_dir: str, edges: list[dict]) -> None:
    g = {"edges": edges}
    (Path(agent_dir) / "memory" / "graph.json").write_text(
        json.dumps(g, ensure_ascii=False), encoding="utf-8"
    )


def test_blocked_name_detection():
    assert _name_is_blocked("SmartTrigger")
    assert _name_is_blocked("smart trigger")
    assert _name_is_blocked("deadline_check")
    assert _name_is_blocked("wiki")
    assert _name_is_blocked("Automated Deadline Management")
    assert not _name_is_blocked("Иван Петров")
    assert not _name_is_blocked("Phase 5")


def test_blocklist_hits_in_pages_and_graph(agent_dir):
    _write_page(agent_dir, "people", "SmartTrigger")
    _write_page(agent_dir, "projects", "Phase5")
    _write_graph(agent_dir, [
        {"from": "deadline_check", "to": "Phase5", "type": "related"},
        {"from": "Phase5", "to": "Иван", "type": "owned_by"},
    ])

    report = lint_wiki(agent_dir)
    codes = {h.code for h in report.blocklist_hits}
    assert "blocklist_entity" in codes
    assert "blocklist_edge" in codes
    assert report.errors >= 2


def test_orphan_page_detection(agent_dir):
    _write_page(agent_dir, "people", "Иван")
    _write_page(agent_dir, "projects", "Phase5")
    _write_graph(agent_dir, [
        {"from": "Phase5", "to": "Acme", "type": "related"},
    ])

    report = lint_wiki(agent_dir)
    orphan_names = {o.where.split("/")[-1].replace(".md", "") for o in report.orphans}
    assert "Иван" in orphan_names
    # Phase5 не orphan — он есть в графе
    assert "Phase5" not in orphan_names


def test_dangling_edge_detection(agent_dir):
    _write_page(agent_dir, "projects", "Phase5")
    _write_graph(agent_dir, [
        {"from": "Phase5", "to": "Acme", "type": "related"},
    ])

    report = lint_wiki(agent_dir)
    dangling_messages = " ".join(d.message for d in report.dangling_edges)
    assert "Acme" in dangling_messages


def test_duplicate_entity_detection(agent_dir):
    _write_page(agent_dir, "people", "Иван")
    _write_page(agent_dir, "projects", "иван")  # тот же stem в lower
    _write_graph(agent_dir, [])

    report = lint_wiki(agent_dir)
    assert len(report.duplicates) == 1
    assert "иван" in report.duplicates[0].message.lower()


def test_exclusive_conflict_detection(agent_dir):
    _write_page(agent_dir, "people", "Иван")
    _write_page(agent_dir, "projects", "Acme")
    _write_page(agent_dir, "projects", "Beta")
    _write_graph(agent_dir, [
        {
            "from": "Иван", "to": "Acme", "type": "works_at",
            "first_seen": "2026-04-10", "id": "id1",
        },
        {
            "from": "Иван", "to": "Beta", "type": "works_at",
            "first_seen": "2026-04-15", "id": "id2",
            # ОБА активны — это и есть конфликт
        },
    ])

    report = lint_wiki(agent_dir)
    assert len(report.contradictions) == 1
    assert "works_at" in report.contradictions[0].message
    assert report.errors >= 1


def test_superseded_edges_dont_trigger_conflict(agent_dir):
    _write_page(agent_dir, "people", "Иван")
    _write_page(agent_dir, "projects", "Acme")
    _write_page(agent_dir, "projects", "Beta")
    _write_graph(agent_dir, [
        {
            "from": "Иван", "to": "Acme", "type": "works_at",
            "first_seen": "2026-04-10", "id": "id1",
            "superseded_by": "id2",
        },
        {
            "from": "Иван", "to": "Beta", "type": "works_at",
            "first_seen": "2026-04-15", "id": "id2",
        },
    ])
    report = lint_wiki(agent_dir)
    assert len(report.contradictions) == 0


def test_clean_wiki_has_zero_issues(agent_dir):
    _write_page(agent_dir, "people", "Иван")
    _write_page(agent_dir, "projects", "Phase5")
    _write_graph(agent_dir, [
        {
            "from": "Иван", "to": "Phase5", "type": "works_on",
            "first_seen": "2026-04-15", "id": "id1",
        },
    ])
    report = lint_wiki(agent_dir)
    assert report.total == 0
    assert report.errors == 0


def test_run_lint_writes_report(agent_dir):
    _write_page(agent_dir, "people", "Иван")
    _write_graph(agent_dir, [])
    run_lint(agent_dir)
    out = Path(agent_dir) / "memory" / "wiki" / ".lint_report.md"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "Wiki Lint Report" in text
    assert "Orphan" in text


def test_synthesis_pages_excluded(agent_dir):
    """synthesis-страницы не считаются за entity и не лезут в orphans."""
    _write_page(agent_dir, "synthesis", "recurring-themes", "# themes\n")
    _write_graph(agent_dir, [])
    report = lint_wiki(agent_dir)
    # synthesis не должен попасть в orphans
    assert all("recurring-themes" not in o.where for o in report.orphans)
