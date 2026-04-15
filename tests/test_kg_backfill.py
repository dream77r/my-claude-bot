"""Тесты двухфазного KG backfill после апдейта."""

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
def summaries(agent_dir):
    """Накинуть 3 саммари — чтобы synthesis-фаза могла запуститься."""
    sdir = Path(agent_dir) / "memory" / "daily" / "summaries"
    for i, d in enumerate(["2026-04-10", "2026-04-11", "2026-04-12"]):
        (sdir / f"{d}.md").write_text(
            f"# Итоги {d}\n\nТемы: ProductX.\nРешения: запуск MVP.\n",
            encoding="utf-8",
        )


@pytest.fixture
def mock_kg_calls(monkeypatch):
    """Мокнуть L1/L2/L3 чтобы не дёргать Claude CLI."""
    calls = {"l1": [], "l2": [], "l3": 0, "lint": 0, "commit": 0}

    async def fake_l1(agent_dir, model="haiku", date=None):
        calls["l1"].append(date.strftime("%Y-%m-%d") if date else None)
        return {"ok": True, "links_found": 0, "entities": []}

    async def fake_l2(agent_dir, model="haiku", date=None):
        calls["l2"].append(date.strftime("%Y-%m-%d") if date else None)
        return {"ok": True, "topics": [], "decisions": []}

    async def fake_l3(agent_dir, model="haiku", max_summaries=30):
        calls["l3"] += 1
        return {"ok": True, "patterns": [], "cross_links": 0}

    def fake_lint(agent_dir):
        calls["lint"] += 1
        return None

    def fake_commit(agent_dir, message):
        calls["commit"] += 1

    monkeypatch.setattr(kg, "link_daily_entities", fake_l1)
    monkeypatch.setattr(kg, "summarize_day", fake_l2)
    monkeypatch.setattr(kg, "synthesize_graph", fake_l3)
    import src.wiki_lint as wl
    monkeypatch.setattr(wl, "run_lint", fake_lint)
    monkeypatch.setattr(kg.memory, "git_commit", fake_commit)
    return calls


def test_backfill_runs_both_phases(agent_dir, dailies, summaries, mock_kg_calls):
    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    assert result["skipped"] is False
    # Линковка
    assert result["linking"]["skipped"] is False
    assert result["linking"]["processed"] == 5
    assert result["linking"]["errors"] == 0
    # Синтез
    assert result["synthesis"]["skipped"] is False
    assert result["synthesis"]["ok"] is True
    # Фактические вызовы
    assert len(mock_kg_calls["l1"]) == 5
    assert len(mock_kg_calls["l2"]) == 5
    assert mock_kg_calls["l3"] == 1


def test_backfill_idempotent_both_phases(agent_dir, dailies, summaries, mock_kg_calls):
    r1 = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    r2 = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    assert r1["skipped"] is False
    assert r2["skipped"] is True
    assert r2["linking"]["skipped"] is True
    assert r2["linking"]["reason"] == "already_done"
    assert r2["synthesis"]["skipped"] is True
    assert r2["synthesis"]["reason"] == "already_done"
    # L1/L2/L3 вызывались только в первый раз
    assert len(mock_kg_calls["l1"]) == 5
    assert mock_kg_calls["l3"] == 1


def test_backfill_linking_marker_contains_metadata(agent_dir, dailies, summaries, mock_kg_calls):
    asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    marker = Path(agent_dir) / "memory" / kg.BACKFILL_MARKER
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["processed"] == 5
    assert "completed_at" in data
    assert data["days_window"] == 7


def test_backfill_synthesis_marker_contains_metadata(agent_dir, dailies, summaries, mock_kg_calls):
    asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    marker = Path(agent_dir) / "memory" / kg.BACKFILL_SYNTHESIS_MARKER
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert "completed_at" in data


def test_backfill_synthesis_skipped_without_summaries(agent_dir, dailies, mock_kg_calls):
    """Если саммари меньше 2 — синтез пропускается, маркер всё равно ставится."""
    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    # Линковка прошла (L2 замокан, саммари в фикстуре не созданы → на диске пусто)
    assert result["linking"]["skipped"] is False
    # Синтез пропущен по причине insufficient_summaries
    assert result["synthesis"]["skipped"] is True
    assert result["synthesis"]["reason"] == "insufficient_summaries"
    assert mock_kg_calls["l3"] == 0
    # Маркер всё равно поставлен
    marker = Path(agent_dir) / "memory" / kg.BACKFILL_SYNTHESIS_MARKER
    assert marker.exists()


def test_backfill_synthesis_runs_independently_after_linking(
    agent_dir, dailies, summaries, mock_kg_calls
):
    """
    Симуляция: пользователь уже прошёл v1 линковку (маркер стоит), но
    synthesis-фаза была добавлена позже. При следующем старте должен
    запуститься только синтез.
    """
    # Проставить linking marker заранее
    marker = Path(agent_dir) / "memory" / kg.BACKFILL_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"processed": 5}', encoding="utf-8")

    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))

    assert result["linking"]["skipped"] is True
    assert result["linking"]["reason"] == "already_done"
    assert result["synthesis"]["skipped"] is False
    assert result["synthesis"]["ok"] is True
    # L1/L2 НЕ вызывались (линковка пропущена), L3 вызвался
    assert len(mock_kg_calls["l1"]) == 0
    assert len(mock_kg_calls["l2"]) == 0
    assert mock_kg_calls["l3"] == 1


def test_backfill_respects_days_window(agent_dir, dailies, summaries, mock_kg_calls):
    result = asyncio.run(kg.maybe_backfill(agent_dir, days=3))
    assert result["linking"]["processed"] == 3


def test_backfill_handles_no_dailies_no_summaries(agent_dir, mock_kg_calls):
    """Пустой агент: обе фазы пропускаются, оба маркера ставятся."""
    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    assert result["skipped"] is True
    assert result["linking"]["skipped"] is True
    assert result["linking"]["reason"] == "no_historical_dailies"
    assert result["synthesis"]["skipped"] is True
    assert (Path(agent_dir) / "memory" / kg.BACKFILL_MARKER).exists()
    assert (Path(agent_dir) / "memory" / kg.BACKFILL_SYNTHESIS_MARKER).exists()


def test_backfill_survives_l1_errors(agent_dir, dailies, summaries, monkeypatch):
    calls = {"l1": 0, "l2": 0, "l3": 0}

    async def flaky_l1(agent_dir, model="haiku", date=None):
        calls["l1"] += 1
        if calls["l1"] == 2:
            raise RuntimeError("simulated failure")
        return {"ok": True}

    async def fake_l2(agent_dir, model="haiku", date=None):
        calls["l2"] += 1
        return {"ok": True}

    async def fake_l3(agent_dir, model="haiku", max_summaries=30):
        calls["l3"] += 1
        return {"ok": True}

    monkeypatch.setattr(kg, "link_daily_entities", flaky_l1)
    monkeypatch.setattr(kg, "summarize_day", fake_l2)
    monkeypatch.setattr(kg, "synthesize_graph", fake_l3)
    import src.wiki_lint as wl
    monkeypatch.setattr(wl, "run_lint", lambda a: None)
    monkeypatch.setattr(kg.memory, "git_commit", lambda a, m: None)

    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    # Линковка: 5 дней, 1 упал, 4 прошли
    assert result["linking"]["errors"] == 1
    assert result["linking"]["processed"] == 4
    # Синтез отработал независимо
    assert result["synthesis"]["ok"] is True
    # Оба маркера поставлены → цикл не крутится
    assert (Path(agent_dir) / "memory" / kg.BACKFILL_MARKER).exists()
    assert (Path(agent_dir) / "memory" / kg.BACKFILL_SYNTHESIS_MARKER).exists()


def test_backfill_survives_l3_errors(agent_dir, dailies, summaries, monkeypatch):
    """L3 упал → маркер всё равно ставится, чтобы не крутиться."""
    async def ok_l1(agent_dir, model="haiku", date=None):
        return {"ok": True}

    async def ok_l2(agent_dir, model="haiku", date=None):
        return {"ok": True}

    async def failing_l3(agent_dir, model="haiku", max_summaries=30):
        raise RuntimeError("L3 blew up")

    monkeypatch.setattr(kg, "link_daily_entities", ok_l1)
    monkeypatch.setattr(kg, "summarize_day", ok_l2)
    monkeypatch.setattr(kg, "synthesize_graph", failing_l3)
    import src.wiki_lint as wl
    monkeypatch.setattr(wl, "run_lint", lambda a: None)
    monkeypatch.setattr(kg.memory, "git_commit", lambda a, m: None)

    result = asyncio.run(kg.maybe_backfill(agent_dir, days=7))
    assert result["synthesis"]["ok"] is False
    assert "L3 blew up" in result["synthesis"]["error"]
    # Маркер всё равно поставлен
    assert (Path(agent_dir) / "memory" / kg.BACKFILL_SYNTHESIS_MARKER).exists()
