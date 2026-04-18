"""Tests for src/dispatcher._publish_one idempotency + inflight recovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src import dispatcher
from src.bus import FleetBus


def _write_dispatch(dispatch_dir: Path, name: str, payload: dict) -> Path:
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    path = dispatch_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_agent_tree(tmp_path: Path, agent: str = "me") -> Path:
    """Создать agents/{agent}/memory/dispatch/ и вернуть agent_dir."""
    agent_dir = tmp_path / "agents" / agent
    (agent_dir / "memory" / "dispatch").mkdir(parents=True)
    return agent_dir


@pytest.mark.asyncio
async def test_publish_success_removes_file(tmp_path: Path) -> None:
    agent_dir = _make_agent_tree(tmp_path)
    dispatch = agent_dir / "memory" / "dispatch"
    path = _write_dispatch(
        dispatch, "a.json", {"chat_id": 111, "text": "hello"}
    )

    bus = FleetBus()
    bus.subscribe("telegram:me")

    ok = await dispatcher._publish_one(path, "me", bus)
    assert ok is True
    assert not path.exists()
    assert not dispatcher._inflight_path(path).exists()


@pytest.mark.asyncio
async def test_no_subscribers_restores_file(tmp_path: Path) -> None:
    agent_dir = _make_agent_tree(tmp_path)
    dispatch = agent_dir / "memory" / "dispatch"
    path = _write_dispatch(
        dispatch, "b.json", {"chat_id": 222, "text": "retry later"}
    )

    bus = FleetBus()  # no subscribers

    ok = await dispatcher._publish_one(path, "me", bus)
    assert ok is False
    assert path.exists(), "file должен вернуться для next cycle"
    assert not dispatcher._inflight_path(path).exists()


@pytest.mark.asyncio
async def test_publish_exception_restores_file(tmp_path: Path) -> None:
    agent_dir = _make_agent_tree(tmp_path)
    dispatch = agent_dir / "memory" / "dispatch"
    path = _write_dispatch(
        dispatch, "c.json", {"chat_id": 333, "text": "boom"}
    )

    bus = FleetBus()
    bus.publish = AsyncMock(side_effect=RuntimeError("bus broke"))

    ok = await dispatcher._publish_one(path, "me", bus)
    assert ok is False
    assert path.exists()
    assert not dispatcher._inflight_path(path).exists()


@pytest.mark.asyncio
async def test_unlink_failure_after_publish_does_not_republish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ключевая защита от дублирующей доставки: если publish прошёл, но
    unlink inflight упал — файл остаётся как .inflight и НЕ попадает в
    следующий scan (*.json)."""
    agent_dir = _make_agent_tree(tmp_path)
    dispatch = agent_dir / "memory" / "dispatch"
    path = _write_dispatch(
        dispatch, "d.json", {"chat_id": 444, "text": "ship it"}
    )

    bus = FleetBus()
    bus.subscribe("telegram:me")

    original_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name.endswith(".inflight"):
            raise OSError("synthetic")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    ok = await dispatcher._publish_one(path, "me", bus)
    # publish был успешен — возвращаем True, несмотря на unlink failure
    assert ok is True
    # Файл уже не *.json — scan его не подхватит
    scan = list(dispatch.glob("*.json"))
    assert scan == []
    # .inflight остался как «мусор», который ок почистить руками
    assert dispatcher._inflight_path(path).exists()


@pytest.mark.asyncio
async def test_invalid_payload_quarantines(tmp_path: Path) -> None:
    agent_dir = _make_agent_tree(tmp_path)
    dispatch = agent_dir / "memory" / "dispatch"
    path = dispatch / "bad.json"
    path.write_text("not json at all {", encoding="utf-8")

    bus = FleetBus()
    bus.subscribe("telegram:me")

    ok = await dispatcher._publish_one(path, "me", bus)
    assert ok is False
    assert not path.exists()
    failed = dispatch / "failed"
    assert failed.exists()
    assert any(failed.iterdir())


def test_recover_inflight_quarantines_orphans(tmp_path: Path) -> None:
    agent_dir = _make_agent_tree(tmp_path)
    dispatch = agent_dir / "memory" / "dispatch"
    orphan = dispatch / "stranded.json.inflight"
    orphan.write_text(
        json.dumps({"chat_id": 1, "text": "old"}), encoding="utf-8"
    )

    dispatcher._recover_inflight(dispatch, str(agent_dir))

    assert not orphan.exists()
    failed = dispatch / "failed"
    assert any(p.name.endswith("stranded.json.inflight") for p in failed.iterdir())


def test_recover_inflight_noop_on_missing_dir(tmp_path: Path) -> None:
    dispatcher._recover_inflight(
        tmp_path / "does-not-exist", str(tmp_path)
    )


@pytest.mark.asyncio
async def test_parallel_publish_never_double_delivers(tmp_path: Path) -> None:
    """Два последовательных _publish_one на один и тот же файл: второй
    должен увидеть rename failure и не доставить сообщение повторно."""
    agent_dir = _make_agent_tree(tmp_path)
    dispatch = agent_dir / "memory" / "dispatch"
    path = _write_dispatch(
        dispatch, "e.json", {"chat_id": 555, "text": "once"}
    )

    bus = FleetBus()
    q = bus.subscribe("telegram:me")

    # Запускаем два publish параллельно на один файл
    results = await asyncio.gather(
        dispatcher._publish_one(path, "me", bus),
        dispatcher._publish_one(path, "me", bus),
        return_exceptions=True,
    )

    # Ровно один должен получить True
    successes = [r for r in results if r is True]
    assert len(successes) == 1, results

    # Только одно сообщение в очереди — никаких дублей
    delivered = []
    while not q.empty():
        delivered.append(q.get_nowait())
    assert len(delivered) == 1
