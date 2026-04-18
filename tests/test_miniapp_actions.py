"""Тесты писем Mini App actions: stop/start/restart агента."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.http_server import create_app
from tests.test_miniapp_auth import FakeAgent, make_init_data

TOKEN_ME = "111:ME_TOKEN"
TOKEN_CODER = "222:CODER_TOKEN"


class FakeRuntime:
    """Runtime-стаб: отслеживает вызовы start_agent/stop_agent."""

    def __init__(self, root: Path, agents: dict):
        self.root = root
        self.agents = agents
        self.workers: dict = {}
        self._running = set(agents.keys())
        self.calls: list[tuple[str, str]] = []  # (method, name)
        self.next_stop: tuple[bool, str] = (True, "stopped")
        self.next_start: tuple[bool, str] = (True, "started")

    def running_agents(self) -> list[str]:
        return list(self._running)

    async def stop_agent(self, name: str):
        self.calls.append(("stop", name))
        ok, msg = self.next_stop
        if ok:
            self._running.discard(name)
        return ok, msg

    async def start_agent(self, name: str):
        self.calls.append(("start", name))
        ok, msg = self.next_start
        if ok:
            self._running.add(name)
        return ok, msg


@pytest.fixture
def fleet(tmp_path: Path):
    root = tmp_path
    (root / "agents").mkdir()

    def mk_agent(name: str, token: str, allowed: list[int], master: bool = False):
        d = root / "agents" / name
        (d / "memory").mkdir(parents=True)
        agent = FakeAgent(name, token, allowed, master=master)
        agent.agent_dir = str(d)
        agent.config = {"name": name, "display_name": name.title()}
        return agent

    me = mk_agent("me", TOKEN_ME, [1001], master=True)
    coder = mk_agent("coder", TOKEN_CODER, [1002])
    runtime = FakeRuntime(root, {"me": me, "coder": coder})
    app = create_app(runtime)
    return root, runtime, TestClient(app)


def founder_hdrs():
    raw = make_init_data(TOKEN_ME, user_id=9999)
    return {"Authorization": f"tma {raw}", "X-Origin-Agent": "me"}


def user_hdrs(user_id: int):
    raw = make_init_data(TOKEN_ME, user_id=user_id)
    return {"Authorization": f"tma {raw}", "X-Origin-Agent": "me"}


# ── founder-only ───────────────────────────────────────────────────────────


class TestFounderGate:
    def test_non_founder_stop_is_forbidden(self, fleet, monkeypatch):
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        _, runtime, client = fleet
        r = client.post("/api/agents/me/stop", headers=user_hdrs(1001))
        assert r.status_code == 403
        assert runtime.calls == []

    def test_non_founder_start_is_forbidden(self, fleet, monkeypatch):
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        _, runtime, client = fleet
        r = client.post("/api/agents/me/start", headers=user_hdrs(1001))
        assert r.status_code == 403
        assert runtime.calls == []

    def test_non_founder_restart_is_forbidden(self, fleet, monkeypatch):
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        _, runtime, client = fleet
        r = client.post("/api/agents/me/restart", headers=user_hdrs(1001))
        assert r.status_code == 403
        assert runtime.calls == []


# ── happy paths ────────────────────────────────────────────────────────────


class TestActions:
    def test_stop_delegates_to_runtime(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, runtime, client = fleet
        r = client.post("/api/agents/coder/stop", headers=founder_hdrs())
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert runtime.calls == [("stop", "coder")]
        assert "coder" not in runtime.running_agents()

    def test_start_delegates_to_runtime(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, runtime, client = fleet
        # эмулируем ранее остановленного
        runtime._running.discard("coder")
        r = client.post("/api/agents/coder/start", headers=founder_hdrs())
        assert r.status_code == 200
        assert runtime.calls == [("start", "coder")]
        assert "coder" in runtime.running_agents()

    def test_restart_stops_then_starts(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, runtime, client = fleet
        r = client.post("/api/agents/coder/restart", headers=founder_hdrs())
        assert r.status_code == 200
        assert runtime.calls == [("stop", "coder"), ("start", "coder")]
        assert "coder" in runtime.running_agents()

    def test_restart_of_stopped_only_starts(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, runtime, client = fleet
        runtime._running.discard("coder")
        r = client.post("/api/agents/coder/restart", headers=founder_hdrs())
        assert r.status_code == 200
        # без stop — агент не был запущен
        assert runtime.calls == [("start", "coder")]


# ── error surfaces ─────────────────────────────────────────────────────────


class TestErrors:
    def test_runtime_refusal_returns_409(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, runtime, client = fleet
        runtime.next_stop = (False, "Агент 'coder' не запущен")
        # даже если running_agents показывает coder — стаб вернёт False
        r = client.post("/api/agents/coder/stop", headers=founder_hdrs())
        assert r.status_code == 409
        assert "не запущен" in r.json()["detail"]

    def test_restart_stop_failure_surfaces(self, fleet, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, runtime, client = fleet
        runtime.next_stop = (False, "busy")
        r = client.post("/api/agents/coder/restart", headers=founder_hdrs())
        assert r.status_code == 409
        assert "stop failed" in r.json()["detail"]
        # start не должен был вызваться
        assert ("start", "coder") not in runtime.calls


# ── Skills install / uninstall / refresh ────────────────────────────────


class _FakeInstallResult:
    def __init__(self, ok=True, error=None, installed_to="/tmp/x",
                 missing_memory=None, has_scripts=False, skill_name=""):
        self.ok = ok
        self.error = error
        self.installed_to = installed_to
        self.missing_memory = missing_memory or []
        self.has_scripts = has_scripts
        self.skill_name = skill_name


class _FakePool:
    def __init__(self):
        self.available = True
        self.install_calls: list[tuple[str, str, bool]] = []
        self.uninstall_calls: list[tuple[str, str]] = []
        self.refresh_calls = 0
        self.next_install = _FakeInstallResult(ok=True)
        self.next_uninstall = True
        self.skills = []

    def is_available(self): return self.available
    def list_skills(self): return list(self.skills)

    def install_skill(self, name, agent_dir, *, overwrite=False, strict_memory=False):
        self.install_calls.append((name, str(agent_dir), overwrite))
        return self.next_install

    def uninstall_skill(self, name, agent_dir):
        self.uninstall_calls.append((name, str(agent_dir)))
        return self.next_uninstall

    def refresh(self): self.refresh_calls += 1


@pytest.fixture
def pool(monkeypatch):
    """Перехватываем make_pool_from_env во всех ветках actions.py."""
    p = _FakePool()
    monkeypatch.setattr("src.miniapp.actions.make_pool_from_env", lambda root: p)
    return p


class TestSkillInstall:
    def test_founder_installs(self, fleet, pool, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, runtime, client = fleet
        r = client.post(
            "/api/agents/coder/skills/install",
            headers=founder_hdrs(),
            json={"skill": "web-research"},
        )
        assert r.status_code == 200
        assert pool.install_calls == [
            ("web-research", str(runtime.agents["coder"].agent_dir), False)
        ]

    def test_owner_can_install_to_own_agent(self, fleet, pool, monkeypatch):
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        _, runtime, client = fleet
        # user 1002 имеет доступ к coder
        raw = make_init_data(TOKEN_ME, user_id=1002)
        hdrs = {"Authorization": f"tma {raw}", "X-Origin-Agent": "me"}
        r = client.post(
            "/api/agents/coder/skills/install",
            headers=hdrs,
            json={"skill": "web-research"},
        )
        assert r.status_code == 200

    def test_stranger_cannot_install(self, fleet, pool, monkeypatch):
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        _, runtime, client = fleet
        # user 1001 имеет доступ только к me
        r = client.post(
            "/api/agents/coder/skills/install",
            headers=user_hdrs(1001),
            json={"skill": "web-research"},
        )
        assert r.status_code == 403
        assert pool.install_calls == []

    def test_empty_skill_rejected(self, fleet, pool, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, _, client = fleet
        r = client.post(
            "/api/agents/coder/skills/install",
            headers=founder_hdrs(),
            json={"skill": "  "},
        )
        assert r.status_code == 400

    def test_pool_unavailable_returns_503(self, fleet, pool, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, _, client = fleet
        pool.available = False
        r = client.post(
            "/api/agents/coder/skills/install",
            headers=founder_hdrs(),
            json={"skill": "web-research"},
        )
        assert r.status_code == 503

    def test_install_failure_returns_409(self, fleet, pool, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, _, client = fleet
        pool.next_install = _FakeInstallResult(ok=False, error="already installed")
        r = client.post(
            "/api/agents/coder/skills/install",
            headers=founder_hdrs(),
            json={"skill": "web-research"},
        )
        assert r.status_code == 409
        assert "already installed" in r.json()["detail"]


class TestSkillUninstall:
    def test_founder_uninstalls(self, fleet, pool, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, runtime, client = fleet
        r = client.post(
            "/api/agents/coder/skills/uninstall",
            headers=founder_hdrs(),
            json={"skill": "web-research"},
        )
        assert r.status_code == 200
        assert pool.uninstall_calls == [
            ("web-research", str(runtime.agents["coder"].agent_dir))
        ]

    def test_not_installed_returns_404(self, fleet, pool, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, _, client = fleet
        pool.next_uninstall = False
        r = client.post(
            "/api/agents/coder/skills/uninstall",
            headers=founder_hdrs(),
            json={"skill": "ghost"},
        )
        assert r.status_code == 404


class TestPoolRefresh:
    def test_founder_refreshes(self, fleet, pool, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "9999")
        _, _, client = fleet
        r = client.post("/api/skills/pool/refresh", headers=founder_hdrs())
        assert r.status_code == 200
        assert pool.refresh_calls == 1

    def test_non_founder_refresh_forbidden(self, fleet, pool, monkeypatch):
        monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        _, _, client = fleet
        r = client.post("/api/skills/pool/refresh", headers=user_hdrs(1001))
        assert r.status_code == 403
        assert pool.refresh_calls == 0
