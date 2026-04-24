"""Тесты A2A Agent Cards endpoint (spec-compliance через a2a-sdk)."""

from __future__ import annotations

from pathlib import Path

import pytest
# a2a-sdk 1.0: AgentCard pydantic-модель живёт в compat.v0_3; cards.py
# строит карточку из этого же слоя — тест должен сравнивать с ним.
from a2a.compat.v0_3.types import AgentCard
from fastapi.testclient import TestClient

from src.a2a.cards import build_agent_card
from src.http_server import create_app
from tests.test_miniapp_auth import FakeAgent


class FakeRuntime:
    def __init__(self, root: Path, agents: dict):
        self.root = root
        self.agents = agents

    def running_agents(self) -> list[str]:
        return list(self.agents)


@pytest.fixture
def fleet(tmp_path: Path):
    root = tmp_path
    (root / "agents").mkdir()

    def mk_agent(name: str, master: bool = False) -> FakeAgent:
        d = root / "agents" / name
        (d / "skills").mkdir(parents=True)
        agent = FakeAgent(name, f"{name}_TOKEN", [1001], master=master)
        agent.agent_dir = str(d)
        agent.config = {
            "name": name,
            "display_name": name.title(),
            "description": f"Agent {name} for tests",
        }
        return agent

    me = mk_agent("me", master=True)
    coder = mk_agent("coder")
    runtime = FakeRuntime(root, {"me": me, "coder": coder})
    return root, runtime, TestClient(create_app(runtime))


# ── build_agent_card (pure, SDK model) ─────────────────────────────────────


class TestBuildAgentCard:
    def test_returns_agent_card_instance(self, fleet):
        root, runtime, _ = fleet
        card = build_agent_card(runtime.agents["me"], "https://example.com")
        assert isinstance(card, AgentCard)
        assert card.name == "me"
        assert card.url == "https://example.com/a2a/me"
        assert card.protocol_version == "0.3.0"
        assert card.capabilities.streaming is True
        assert card.default_input_modes == ["text/plain"]
        assert card.provider.organization == "my-claude-bot"
        assert card.provider.url == "https://example.com"

    def test_base_url_trailing_slash_stripped(self, fleet):
        root, runtime, _ = fleet
        card = build_agent_card(runtime.agents["me"], "https://example.com/")
        assert card.url == "https://example.com/a2a/me"
        assert card.provider.url == "https://example.com"

    def test_skills_discovered(self, fleet):
        root, runtime, _ = fleet
        skills = Path(runtime.agents["me"].agent_dir) / "skills"
        (skills / "web-research.md").write_text(
            "---\n"
            "name: web-research\n"
            "description: Research stuff\n"
            "tags: [research, web]\n"
            "---\nbody\n"
        )
        card = build_agent_card(runtime.agents["me"], "https://example.com")
        assert len(card.skills) == 1
        s = card.skills[0]
        assert s.id == "web-research"
        assert s.description == "Research stuff"
        assert set(s.tags) == {"research", "web"}

    def test_bundle_skill_discovered(self, fleet):
        root, runtime, _ = fleet
        skills = Path(runtime.agents["me"].agent_dir) / "skills"
        bundle = skills / "deep-research"
        bundle.mkdir()
        (bundle / "SKILL.md").write_text(
            "---\nname: deep-research\ndescription: Deep dive\n---\n"
        )
        card = build_agent_card(runtime.agents["me"], "https://example.com")
        ids = {s.id for s in card.skills}
        assert "deep-research" in ids

    def test_no_skills_dir_returns_empty_list(self, tmp_path: Path):
        agent = FakeAgent("ghost", "T", [], master=False)
        agent.agent_dir = str(tmp_path / "nope")
        agent.config = {"name": "ghost", "display_name": "Ghost"}
        card = build_agent_card(agent, "https://example.com")
        assert card.skills == []


# ── HTTP endpoint spec compliance ──────────────────────────────────────────


class TestAgentCardEndpoint:
    def test_unauthenticated_public_access(self, fleet):
        root, runtime, client = fleet
        r = client.get("/.well-known/agent-card/me")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "me"
        # Spec-compliant camelCase aliases:
        assert "protocolVersion" in body
        assert "defaultInputModes" in body
        assert "defaultOutputModes" in body
        assert body["provider"]["organization"] == "my-claude-bot"

    def test_response_parseable_by_sdk_resolver(self, fleet):
        """JSON ответа должен распарситься обратно в AgentCard без ошибок."""
        root, runtime, client = fleet
        r = client.get("/.well-known/agent-card/me")
        assert r.status_code == 200
        parsed = AgentCard.model_validate(r.json())
        assert parsed.name == "me"

    def test_unknown_agent_404(self, fleet):
        root, runtime, client = fleet
        r = client.get("/.well-known/agent-card/ghost")
        assert r.status_code == 404

    def test_public_base_url_env_overrides(self, fleet, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://prod.example.com")
        root, runtime, client = fleet
        r = client.get("/.well-known/agent-card/me")
        body = r.json()
        assert body["url"] == "https://prod.example.com/a2a/me"
        assert body["provider"]["url"] == "https://prod.example.com"

    def test_index_lists_all_running(self, fleet, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://prod.example.com")
        root, runtime, client = fleet
        r = client.get("/.well-known/agent-cards")
        assert r.status_code == 200
        names = {c["name"] for c in r.json()["cards"]}
        assert names == {"me", "coder"}
        urls = {c["url"] for c in r.json()["cards"]}
        assert "https://prod.example.com/.well-known/agent-card/me" in urls
