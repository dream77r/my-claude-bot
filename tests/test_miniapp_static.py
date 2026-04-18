"""Тесты раздачи статики Mini App через FastAPI StaticFiles."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.http_server import create_app
from tests.test_miniapp_auth import FakeAgent


class FakeRuntime:
    def __init__(self, root: Path, agents: dict):
        self.root = root
        self.agents = agents
        self.workers = {}

    def running_agents(self) -> list[str]:
        return list(self.agents)


@pytest.fixture
def client(tmp_path: Path):
    (tmp_path / "agents").mkdir()
    me_dir = tmp_path / "agents" / "me"
    (me_dir / "memory").mkdir(parents=True)
    (me_dir / "skills").mkdir()
    me = FakeAgent("me", "ME_TOKEN", [1001], master=True)
    me.agent_dir = str(me_dir)
    me.config = {"name": "me", "display_name": "Me"}
    runtime = FakeRuntime(tmp_path, {"me": me})
    app = create_app(runtime)
    return TestClient(app)


class TestMiniAppStatic:
    def test_index_served(self, client):
        r = client.get("/miniapp/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Cockpit" in r.text
        # SDK подключён
        assert "telegram-web-app.js" in r.text

    def test_app_js_served(self, client):
        r = client.get("/miniapp/assets/app.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert "/api/stats" in r.text

    def test_styles_css_served(self, client):
        r = client.get("/miniapp/assets/styles.css")
        assert r.status_code == 200
        assert "text/css" in r.headers["content-type"]
        assert "tg-theme" in r.text

    def test_unknown_miniapp_path_404(self, client):
        r = client.get("/miniapp/does-not-exist.png")
        assert r.status_code == 404

    def test_env_override(self, tmp_path: Path, monkeypatch):
        """MINIAPP_DIR env переопределяет расположение."""
        custom = tmp_path / "custom"
        custom.mkdir()
        (custom / "index.html").write_text("<p>custom</p>")
        monkeypatch.setenv("MINIAPP_DIR", str(custom))

        (tmp_path / "agents").mkdir()
        me_dir = tmp_path / "agents" / "me"
        (me_dir / "memory").mkdir(parents=True)
        me = FakeAgent("me", "T", [1001], master=True)
        me.agent_dir = str(me_dir)
        me.config = {"name": "me", "display_name": "Me"}
        runtime = FakeRuntime(tmp_path, {"me": me})
        app = create_app(runtime)
        c = TestClient(app)

        r = c.get("/miniapp/")
        assert r.status_code == 200
        assert "custom" in r.text
