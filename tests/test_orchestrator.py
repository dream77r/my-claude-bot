"""Тесты для orchestrator.py."""

import asyncio
from unittest.mock import MagicMock

import pytest

from src.bus import FleetBus, FleetMessage, MessageType
from src.orchestrator import Orchestrator


@pytest.fixture
def bus():
    return FleetBus()


def make_mock_agent(name: str):
    agent = MagicMock()
    agent.name = name
    agent.agent_dir = f"/tmp/agents/{name}"
    return agent


class TestOrchestrator:
    def test_single_agent(self, bus):
        agents = {"me": make_mock_agent("me")}
        orch = Orchestrator(bus, agents)
        assert orch.is_single_agent is True

    def test_multi_agent(self, bus):
        agents = {
            "me": make_mock_agent("me"),
            "coder": make_mock_agent("coder"),
        }
        orch = Orchestrator(bus, agents)
        assert orch.is_single_agent is False

    def test_register_chat(self, bus):
        agents = {"me": make_mock_agent("me")}
        orch = Orchestrator(bus, agents)
        orch.register_chat(12345, "me")
        assert orch._chat_agent_map[12345] == "me"


class TestResolveAgent:
    def test_explicit_target(self, bus):
        agents = {
            "me": make_mock_agent("me"),
            "coder": make_mock_agent("coder"),
        }
        orch = Orchestrator(bus, agents)
        msg = FleetMessage(
            source="telegram", target="agent:coder", content="test"
        )
        assert orch.resolve_agent(msg) == "coder"

    def test_unknown_agent(self, bus):
        agents = {"me": make_mock_agent("me")}
        orch = Orchestrator(bus, agents)
        msg = FleetMessage(
            source="telegram", target="agent:unknown", content="test"
        )
        assert orch.resolve_agent(msg) is None

    def test_by_chat_id(self, bus):
        agents = {
            "me": make_mock_agent("me"),
            "coder": make_mock_agent("coder"),
        }
        orch = Orchestrator(bus, agents)
        orch.register_chat(99999, "coder")
        msg = FleetMessage(
            source="telegram", target="orchestrator",
            content="test", chat_id=99999,
        )
        assert orch.resolve_agent(msg) == "coder"

    def test_single_agent_fallback(self, bus):
        agents = {"me": make_mock_agent("me")}
        orch = Orchestrator(bus, agents)
        msg = FleetMessage(
            source="telegram", target="orchestrator", content="test"
        )
        assert orch.resolve_agent(msg) == "me"

    def test_multi_agent_no_match(self, bus):
        agents = {
            "me": make_mock_agent("me"),
            "coder": make_mock_agent("coder"),
        }
        orch = Orchestrator(bus, agents)
        msg = FleetMessage(
            source="telegram", target="orchestrator",
            content="test", chat_id=0,
        )
        assert orch.resolve_agent(msg) is None


class TestRouteMessage:
    @pytest.mark.asyncio
    async def test_routes_to_agent(self, bus):
        agents = {"me": make_mock_agent("me")}
        orch = Orchestrator(bus, agents)
        agent_q = bus.subscribe("agent:me")

        msg = FleetMessage(
            source="telegram", target="orchestrator",
            content="hello", chat_id=0,
        )
        ok = await orch.route_message(msg)
        assert ok is True
        assert not agent_q.empty()
        routed = agent_q.get_nowait()
        assert routed.content == "hello"
        assert routed.target == "agent:me"
        assert routed.metadata["routed_by"] == "orchestrator"

    @pytest.mark.asyncio
    async def test_route_failure(self, bus):
        agents = {
            "me": make_mock_agent("me"),
            "coder": make_mock_agent("coder"),
        }
        orch = Orchestrator(bus, agents)
        msg = FleetMessage(
            source="telegram", target="orchestrator",
            content="test", chat_id=0,
        )
        ok = await orch.route_message(msg)
        assert ok is False
