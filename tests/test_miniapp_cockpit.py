"""Тесты Cockpit API: status, activity, stats."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.http_server import create_app
from tests.test_miniapp_auth import FakeAgent, make_init_data

TOKEN_ME = "111:ME_TOKEN"
TOKEN_CODER = "222:CODER_TOKEN"


class _FakeWorker:
    """Минимальный стаб AgentWorker — только нужные cockpit'у методы."""

    def __init__(self, busy: bool = False):
        self._busy = busy
        self._active_tasks: dict[int, object] = {}
        if busy:
            # Эмулируем running task через флаг
            class _T:
                def done(self_inner):
                    return False

                def get_name(self_inner):
                    return "task-1"

            self._active_tasks[42] = _T()

    def is_busy(self) -> bool:
        return self._busy

    def active_info(self):
        return [
            {"chat_id": cid, "name": t.get_name()}
            for cid, t in self._active_tasks.items()
            if not t.done()
        ]


class FakeRuntime:
    def __init__(self, root: Path, agents: dict, workers: dict):
        self.root = root
        self.agents = agents
        self.workers = workers

    def running_agents(self) -> list[str]:
        return list(self.agents)


@pytest.fixture
def fleet(tmp_path: Path):
    root = tmp_path
    (root / "agents").mkdir()

    def mk_agent(name: str, token: str, allowed: list[int], master: bool = False):
        d = root / "agents" / name
        (d / "memory").mkdir(parents=True)
        (d / "skills").mkdir()
        agent = FakeAgent(name, token, allowed, master=master)
        agent.agent_dir = str(d)
        agent.config = {"name": name, "display_name": name.title()}
        return agent

    me = mk_agent("me", TOKEN_ME, [1001], master=True)
    coder = mk_agent("coder", TOKEN_CODER, [1002])
    workers = {"me": _FakeWorker(busy=False), "coder": _FakeWorker(busy=True)}
    runtime = FakeRuntime(root, {"me": me, "coder": coder}, workers)
    app = create_app(runtime)
    return root, runtime, TestClient(app)


def auth_hdrs(token: str, user_id: int):
    return {
        "Authorization": f"tma {make_init_data(token, user_id=user_id)}",
        "X-Origin-Agent": "me",
    }


# ── /status ────────────────────────────────────────────────────────────────


class TestAgentStatus:
    def test_idle_agent(self, fleet):
        root, runtime, client = fleet
        r = client.get("/api/agents/me/status", headers=auth_hdrs(TOKEN_ME, 1001))
        assert r.status_code == 200
        body = r.json()
        assert body["running"] is True
        assert body["busy"] is False
        assert body["active_count"] == 0
        assert body["display_name"] == "Me"

    def test_busy_agent_requires_access(self, fleet, monkeypatch):
        """user 1001 → только me, к coder нет доступа → 403."""
        root, runtime, client = fleet
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        r = client.get("/api/agents/coder/status", headers=auth_hdrs(TOKEN_ME, 1001))
        assert r.status_code == 403

    def test_busy_agent_as_founder(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        root, runtime, client = fleet
        raw = make_init_data(TOKEN_ME, user_id=9999)
        r = client.get(
            "/api/agents/coder/status",
            headers={"Authorization": f"tma {raw}", "X-Origin-Agent": "me"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["busy"] is True
        assert body["active_count"] == 1
        assert body["active"][0]["chat_id"] == 42

    def test_unknown_agent_404(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        root, runtime, client = fleet
        raw = make_init_data(TOKEN_ME, user_id=9999)
        r = client.get(
            "/api/agents/ghost/status",
            headers={"Authorization": f"tma {raw}", "X-Origin-Agent": "me"},
        )
        assert r.status_code == 404


# ── /activity ──────────────────────────────────────────────────────────────


class TestActivityFeed:
    def _write_log(self, agent, lines: list[str]):
        log = Path(agent.agent_dir) / "memory" / "log.md"
        log.write_text("\n".join(lines) + "\n")

    def test_empty_feed(self, fleet):
        root, runtime, client = fleet
        r = client.get("/api/activity", headers=auth_hdrs(TOKEN_ME, 1001))
        assert r.status_code == 200
        assert r.json()["events"] == []

    def test_parses_log_and_sorts_desc(self, fleet):
        root, runtime, client = fleet
        self._write_log(
            runtime.agents["me"],
            [
                "# Лог",
                "- [2026-04-18 10:05] user: hello",
                "- [2026-04-18 10:06] assistant: hi",
                "- [2026-04-18 10:07] user: how are you",
            ],
        )
        r = client.get("/api/activity", headers=auth_hdrs(TOKEN_ME, 1001))
        events = r.json()["events"]
        assert [e["ts"] for e in events] == [
            "2026-04-18T10:07",
            "2026-04-18T10:06",
            "2026-04-18T10:05",
        ]
        assert events[0]["agent"] == "me"
        assert events[0]["role"] == "user"
        assert events[0]["preview"] == "how are you"

    def test_limit_applied(self, fleet):
        root, runtime, client = fleet
        lines = ["# Лог"] + [
            f"- [2026-04-18 {h:02d}:00] user: msg{h}" for h in range(5, 15)
        ]
        self._write_log(runtime.agents["me"], lines)
        r = client.get(
            "/api/activity?limit=3",
            headers=auth_hdrs(TOKEN_ME, 1001),
        )
        events = r.json()["events"]
        assert len(events) == 3
        assert events[0]["preview"] == "msg14"

    def test_preview_truncated(self, fleet):
        root, runtime, client = fleet
        long_text = "x" * 500
        self._write_log(
            runtime.agents["me"],
            [f"- [2026-04-18 10:00] user: {long_text}"],
        )
        r = client.get("/api/activity", headers=auth_hdrs(TOKEN_ME, 1001))
        preview = r.json()["events"][0]["preview"]
        assert len(preview) <= 201
        assert preview.endswith("…")

    def test_aggregates_across_accessible_agents(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        root, runtime, client = fleet
        self._write_log(
            runtime.agents["me"],
            ["# Лог", "- [2026-04-18 10:00] user: from me"],
        )
        self._write_log(
            runtime.agents["coder"],
            ["# Лог", "- [2026-04-18 10:01] user: from coder"],
        )
        raw = make_init_data(TOKEN_ME, user_id=9999)
        r = client.get(
            "/api/activity",
            headers={"Authorization": f"tma {raw}", "X-Origin-Agent": "me"},
        )
        events = r.json()["events"]
        assert {e["agent"] for e in events} == {"me", "coder"}

    def test_filter_by_agent(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        root, runtime, client = fleet
        self._write_log(
            runtime.agents["me"],
            ["# Лог", "- [2026-04-18 10:00] user: from me"],
        )
        self._write_log(
            runtime.agents["coder"],
            ["# Лог", "- [2026-04-18 10:01] user: from coder"],
        )
        raw = make_init_data(TOKEN_ME, user_id=9999)
        r = client.get(
            "/api/activity?agent=coder",
            headers={"Authorization": f"tma {raw}", "X-Origin-Agent": "me"},
        )
        events = r.json()["events"]
        assert len(events) == 1
        assert events[0]["agent"] == "coder"

    def test_filter_agent_without_access_403(self, fleet, monkeypatch):
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        root, runtime, client = fleet
        r = client.get(
            "/api/activity?agent=coder",
            headers=auth_hdrs(TOKEN_ME, 1001),
        )
        assert r.status_code == 403


# ── /stats ─────────────────────────────────────────────────────────────────


class TestFleetStats:
    def _write_usage(self, agent, entries: list[dict]):
        stats_dir = Path(agent.agent_dir) / "memory" / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        with open(stats_dir / "usage.jsonl", "a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def test_empty_stats(self, fleet):
        root, runtime, client = fleet
        r = client.get("/api/stats", headers=auth_hdrs(TOKEN_ME, 1001))
        assert r.status_code == 200
        body = r.json()
        assert body["period"] == "today"
        assert body["days"] == 1
        assert body["totals"]["total_calls"] == 0

    def test_invalid_period(self, fleet):
        root, runtime, client = fleet
        r = client.get(
            "/api/stats?period=decade",
            headers=auth_hdrs(TOKEN_ME, 1001),
        )
        assert r.status_code == 400

    def test_aggregates_across_agents(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        root, runtime, client = fleet
        now_iso = datetime.now().isoformat()
        self._write_usage(runtime.agents["me"], [
            {"ts": now_iso, "model": "sonnet", "latency_s": 2.0,
             "tool_calls": 1, "prompt_chars": 100, "response_chars": 200},
            {"ts": now_iso, "model": "haiku", "latency_s": 1.0,
             "tool_calls": 0, "prompt_chars": 50, "response_chars": 100,
             "error": "timeout"},
        ])
        self._write_usage(runtime.agents["coder"], [
            {"ts": now_iso, "model": "sonnet", "latency_s": 3.0,
             "tool_calls": 2, "prompt_chars": 200, "response_chars": 400},
        ])

        raw = make_init_data(TOKEN_ME, user_id=9999)
        r = client.get(
            "/api/stats",
            headers={"Authorization": f"tma {raw}", "X-Origin-Agent": "me"},
        )
        assert r.status_code == 200
        body = r.json()
        totals = body["totals"]
        assert totals["total_calls"] == 3
        assert totals["errors"] == 1
        assert totals["tool_calls"] == 3
        assert totals["total_prompt_chars"] == 350
        assert totals["total_response_chars"] == 700
        assert set(body["by_agent"].keys()) == {"me", "coder"}

    def test_week_period(self, fleet):
        root, runtime, client = fleet
        r = client.get(
            "/api/stats?period=week",
            headers=auth_hdrs(TOKEN_ME, 1001),
        )
        assert r.status_code == 200
        assert r.json()["days"] == 7


# ── AgentWorker.is_busy/active_info sanity ────────────────────────────────


def test_worker_is_busy_active_info_idle():
    from src.agent_worker import AgentWorker
    import asyncio
    from unittest.mock import MagicMock

    w = AgentWorker.__new__(AgentWorker)
    w._active_tasks = {}
    assert w.is_busy() is False
    assert w.active_info() == []


@pytest.mark.asyncio
async def test_worker_is_busy_active_info_busy():
    from src.agent_worker import AgentWorker

    w = AgentWorker.__new__(AgentWorker)

    async def _blocker():
        await asyncio.sleep(0.2)

    task = asyncio.create_task(_blocker(), name="task-alpha")
    w._active_tasks = {77: task}
    try:
        assert w.is_busy() is True
        info = w.active_info()
        assert info == [{"chat_id": 77, "name": "task-alpha"}]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
