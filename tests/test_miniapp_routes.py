"""Тесты read-only API: memory tree/file, skills, skill pool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.http_server import create_app
from tests.test_miniapp_auth import FakeAgent, make_init_data


TOKEN_ME = "111:ME_TOKEN"
TOKEN_CODER = "222:CODER_TOKEN"


class FakeRuntime:
    def __init__(self, root: Path, agents: dict):
        self.root = root
        self.agents = agents

    def running_agents(self) -> list[str]:
        return list(self.agents)


@pytest.fixture
def fleet(tmp_path: Path):
    """Создать флот: me (master, all access), coder (with user 1001)."""
    root = tmp_path
    (root / "agents").mkdir()

    def mk_agent(name: str, token: str, allowed: list[int], master: bool = False):
        agent_dir = root / "agents" / name
        (agent_dir / "memory").mkdir(parents=True)
        (agent_dir / "skills").mkdir()
        agent = FakeAgent(name, token, allowed, master=master)
        agent.agent_dir = str(agent_dir)
        return agent

    me = mk_agent("me", TOKEN_ME, [1001], master=True)
    coder = mk_agent("coder", TOKEN_CODER, [1002])

    runtime = FakeRuntime(root, {"me": me, "coder": coder})
    app = create_app(runtime)
    return root, runtime, TestClient(app)


def auth_headers(agent_token: str, user_id: int) -> dict[str, str]:
    raw = make_init_data(agent_token, user_id=user_id)
    return {
        "Authorization": f"tma {raw}",
        "X-Origin-Agent": "me",
    }


# ── Memory tree ────────────────────────────────────────────────────────────


class TestMemoryTree:
    def test_empty_memory_returns_empty_nodes(self, fleet):
        root, runtime, client = fleet
        r = client.get(
            "/api/agents/me/memory/tree",
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 200
        assert r.json()["nodes"] == []

    def test_lists_files_and_dirs(self, fleet):
        root, runtime, client = fleet
        mem = Path(runtime.agents["me"].agent_dir) / "memory"
        (mem / "wiki").mkdir()
        (mem / "index.md").write_text("# Index")
        (mem / "wiki" / "foo.md").write_text("# Foo")

        r = client.get(
            "/api/agents/me/memory/tree",
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 200
        paths = {n["path"]: n for n in r.json()["nodes"]}
        assert paths["index.md"]["type"] == "file"
        assert paths["index.md"]["size"] == len("# Index")
        assert paths["wiki"]["type"] == "dir"
        assert paths["wiki/foo.md"]["type"] == "file"

    def test_excludes_sensitive_dirs(self, fleet):
        root, runtime, client = fleet
        mem = Path(runtime.agents["me"].agent_dir) / "memory"
        (mem / "sessions").mkdir()
        (mem / "sessions" / "secret.json").write_text('{"token":"abc"}')
        (mem / "outbox").mkdir()
        (mem / "outbox" / "msg.json").write_text("{}")
        (mem / "index.md").write_text("ok")

        r = client.get(
            "/api/agents/me/memory/tree",
            headers=auth_headers(TOKEN_ME, 1001),
        )
        paths = {n["path"] for n in r.json()["nodes"]}
        assert "index.md" in paths
        assert not any(p.startswith("sessions") for p in paths)
        assert not any(p.startswith("outbox") for p in paths)

    def test_forbidden_agent_returns_403(self, fleet):
        """user 1001 is allowed for 'me' but not 'coder'."""
        root, runtime, client = fleet
        raw = make_init_data(TOKEN_ME, user_id=1001)
        r = client.get(
            "/api/agents/coder/memory/tree",
            headers={
                "Authorization": f"tma {raw}",
                "X-Origin-Agent": "me",
            },
        )
        assert r.status_code == 403

    def test_unknown_agent_404(self, fleet):
        root, runtime, client = fleet
        r = client.get(
            "/api/agents/ghost/memory/tree",
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 404


# ── Memory file ────────────────────────────────────────────────────────────


class TestMemoryFile:
    def test_reads_file(self, fleet):
        root, runtime, client = fleet
        mem = Path(runtime.agents["me"].agent_dir) / "memory"
        (mem / "profile.md").write_text("# Profile\nHello")

        r = client.get(
            "/api/agents/me/memory/file",
            params={"path": "profile.md"},
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["content"] == "# Profile\nHello"
        assert body["size"] == len("# Profile\nHello")
        assert body["path"] == "profile.md"

    def test_reads_nested_file(self, fleet):
        root, runtime, client = fleet
        mem = Path(runtime.agents["me"].agent_dir) / "memory"
        (mem / "wiki" / "entities").mkdir(parents=True)
        target = mem / "wiki" / "entities" / "alice.md"
        target.write_text("Alice")

        r = client.get(
            "/api/agents/me/memory/file",
            params={"path": "wiki/entities/alice.md"},
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 200
        assert r.json()["content"] == "Alice"

    def test_path_traversal_rejected(self, fleet):
        root, runtime, client = fleet
        # Создадим файл вне memory — попытаемся прочитать через ..
        outside = root / "secret.md"
        outside.write_text("TOP SECRET")

        for bad in ["../secret.md", "../../etc/passwd", "/etc/passwd"]:
            r = client.get(
                "/api/agents/me/memory/file",
                params={"path": bad},
                headers=auth_headers(TOKEN_ME, 1001),
            )
            assert r.status_code == 400, f"path={bad!r} returned {r.status_code}"

    def test_sensitive_prefix_forbidden(self, fleet):
        root, runtime, client = fleet
        mem = Path(runtime.agents["me"].agent_dir) / "memory"
        (mem / "sessions").mkdir()
        (mem / "sessions" / "token.json").write_text('{"t":1}')

        r = client.get(
            "/api/agents/me/memory/file",
            params={"path": "sessions/token.json"},
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 403

    def test_disallowed_extension(self, fleet):
        root, runtime, client = fleet
        mem = Path(runtime.agents["me"].agent_dir) / "memory"
        (mem / "raw" / "files").mkdir(parents=True)
        (mem / "raw" / "files" / "photo.png").write_bytes(b"\x89PNG\r\n")

        r = client.get(
            "/api/agents/me/memory/file",
            params={"path": "raw/files/photo.png"},
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 415

    def test_nonexistent_file_404(self, fleet):
        root, runtime, client = fleet
        r = client.get(
            "/api/agents/me/memory/file",
            params={"path": "nope.md"},
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 404

    def test_directory_path_400(self, fleet):
        root, runtime, client = fleet
        mem = Path(runtime.agents["me"].agent_dir) / "memory"
        (mem / "wiki").mkdir()
        r = client.get(
            "/api/agents/me/memory/file",
            params={"path": "wiki"},
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 400

    def test_symlink_disallowed(self, fleet, tmp_path):
        root, runtime, client = fleet
        mem = Path(runtime.agents["me"].agent_dir) / "memory"
        real = tmp_path / "real.md"
        real.write_text("real content")
        link = mem / "link.md"
        try:
            link.symlink_to(real)
        except OSError:
            pytest.skip("symlinks not supported on this fs")

        r = client.get(
            "/api/agents/me/memory/file",
            params={"path": "link.md"},
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 400


# ── Skills ────────────────────────────────────────────────────────────────


class TestAgentSkills:
    def test_empty_skills(self, fleet):
        root, runtime, client = fleet
        r = client.get(
            "/api/agents/me/skills",
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 200
        assert r.json()["skills"] == []

    def test_parses_skill_frontmatter(self, fleet):
        root, runtime, client = fleet
        skills = Path(runtime.agents["me"].agent_dir) / "skills"
        (skills / "web-research.md").write_text(
            "---\n"
            "name: web-research\n"
            "description: Research stuff on the web\n"
            "version: 1.2.0\n"
            "tags: [research, web]\n"
            "---\n"
            "# body\n"
        )
        r = client.get(
            "/api/agents/me/skills",
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 200
        items = r.json()["skills"]
        assert len(items) == 1
        skill = items[0]
        assert skill["name"] == "web-research"
        assert skill["description"] == "Research stuff on the web"
        assert skill["version"] == "1.2.0"
        assert set(skill["tags"]) == {"research", "web"}

    def test_bundle_skill_discovered(self, fleet):
        root, runtime, client = fleet
        skills = Path(runtime.agents["me"].agent_dir) / "skills"
        bundle = skills / "my-bundle"
        bundle.mkdir()
        (bundle / "SKILL.md").write_text(
            "---\nname: my-bundle\ndescription: bundle skill\nversion: 2.0.0\n---\n"
        )
        r = client.get(
            "/api/agents/me/skills",
            headers=auth_headers(TOKEN_ME, 1001),
        )
        items = r.json()["skills"]
        bundles = [s for s in items if s.get("bundle")]
        assert len(bundles) == 1
        assert bundles[0]["name"] == "my-bundle"

    def test_skills_forbidden_for_other_agent(self, fleet):
        root, runtime, client = fleet
        r = client.get(
            "/api/agents/coder/skills",
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 403


# ── Skill pool ────────────────────────────────────────────────────────────


class TestSkillPool:
    def test_disabled_pool_returns_empty(self, fleet, monkeypatch):
        monkeypatch.setenv("SKILL_POOL_URL", "disabled")
        root, runtime, client = fleet
        r = client.get(
            "/api/skills/pool",
            headers=auth_headers(TOKEN_ME, 1001),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["skills"] == []

    def test_pool_requires_auth(self, fleet):
        root, runtime, client = fleet
        r = client.get("/api/skills/pool")
        assert r.status_code == 401
