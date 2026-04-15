"""Тесты одноразового KG backfill после апдейта."""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src import knowledge_graph as kg


@pytest.fixture
def agent_dir(tmp_path):
    agent = tmp_path / "agents" / "me"
    mem = agent / "memory"
    for d in [
        "daily", "daily/summaries", "wiki/people", "wiki/projects",
        "wiki/synthesis", "sessions",
    ]:
        (mem / d).mkdir(parents=True, exist_ok=True)
    (mem / "profile.md").write_text("# Profile\n")
    (mem / "index.md").write_text("# Index\n")
    return str(agent)


@pytest.fixture
def dailies(agent_dir):
    """Создать 5 исторических дневников с реальным user-диалогом."""
    today = datetime.now().date()
    for i in range(1, 6):
        d = today - timedelta(days=i)
        (Path(agent_dir) / "memory" / "daily" / f"{d.isoformat()}.md").write_text(
            f"# {d.isoformat()}\n\n"
            f"**10:00** 👤 Обсуждали Phase 5 с Иваном {i} дней назад\n"
            f"**10:01** 🤖 Записал в план\n",
            encoding="utf-8",
        )
    return today


@pytest.fixture
def mock_kg_calls(monkeypatch):
    """Мокнуть L1/L2 чтобы не дёргать Claude CLI."""
    calls = {"l1": [], "l2": [], "lint": 0, "commit": 0}

    async def fake_l1(agent_dir, model="haiku", date=None):
        calls["l1"].append(date.strftime("%Y-%m-%d") if date else None)
        return {"ok": True, "links_found": 0, "entities": []}

    async def fake_l2(agent_dir, model="haiku", date=None):
        calls["l2"].append(date.strftime("%Y-%m-%d") if date else None)
        return {"ok": True, "topics": [], "decisions": []}

    def fake_lint(agent_dir):
        calls["lint"] += 1
        return None

    def fake_commit(agent_dir, message):
        calls["commit"] += 1

    monkeypatch.setattr(kg, "link_daily_entities", fake_l1)
    monkeypatch.setattr(kg, "summarize_day", fake_l2)
    import src.wiki_lint as wl
    monkeypatch.setattr(wl, "run_lint", fake_lint)
    monkeypatch.setattr(kg.memory, "git_commit", fake_commit)
    return calls


def test_backfill_processes_historical_dailies(agent_dir, dailies, mock_kg_calls):
    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    assert result["skipped"] is False
    assert result["processed"] == 5
    assert result["errors"] == 0
    assert len(mock_kg_calls["l1"]) == 5
    assert len(mock_kg_calls["l2"]) == 5


def test_backfill_idempotent(agent_dir, dailies, mock_kg_calls):
    r1 = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    r2 = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    assert r1["skipped"] is False
    assert r2["skipped"] is True
    assert r2["reason"] == "already_done"
    # L1 вызывался только в первый раз
    assert len(mock_kg_calls["l1"]) == 5


def test_backfill_marker_contains_metadata(agent_dir, dailies, mock_kg_calls):
    asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    marker = Path(agent_dir) / "memory" / kg.BACKFILL_MARKER
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["processed"] == 5
    assert "completed_at" in data
    assert data["days_window"] == 7


def test_backfill_respects_days_window(agent_dir, dailies, mock_kg_calls):
    """Только 3 последних дня попадают при days=3."""
    result = asyncio.run(kg.maybe_backfill(agent_dir, days=3))
    assert result["processed"] == 3


def test_backfill_handles_no_dailies(agent_dir, mock_kg_calls):
    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    assert result["skipped"] is True
    assert result["reason"] == "no_historical_dailies"
    # Маркер всё равно ставится
    marker = Path(agent_dir) / "memory" / kg.BACKFILL_MARKER
    assert marker.exists()


def test_backfill_skips_today(agent_dir, mock_kg_calls):
    """Сегодняшний daily не обрабатывается — это работа ночного цикла."""
    today = datetime.now().date()
    (Path(agent_dir) / "memory" / "daily" / f"{today.isoformat()}.md").write_text(
        "**10:00** 👤 Сегодня\n"
    )
    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    assert result["skipped"] is True
    assert result["reason"] == "no_historical_dailies"


def test_backfill_survives_l1_errors(agent_dir, dailies, monkeypatch):
    calls = {"l1": 0, "l2": 0}

    async def flaky_l1(agent_dir, model="haiku", date=None):
        calls["l1"] += 1
        if calls["l1"] == 2:
            raise RuntimeError("simulated failure")
        return {"ok": True}

    async def fake_l2(agent_dir, model="haiku", date=None):
        calls["l2"] += 1
        return {"ok": True}

    monkeypatch.setattr(kg, "link_daily_entities", flaky_l1)
    monkeypatch.setattr(kg, "summarize_day", fake_l2)
    import src.wiki_lint as wl
    monkeypatch.setattr(wl, "run_lint", lambda a: None)
    monkeypatch.setattr(kg.memory, "git_commit", lambda a, m: None)

    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    # 5 дней: 1 упал, 4 прошли
    assert result["errors"] == 1
    assert result["processed"] == 4
    # Маркер всё равно поставлен → не крутим цикл бесконечно
    marker = Path(agent_dir) / "memory" / kg.BACKFILL_MARKER
    assert marker.exists()
