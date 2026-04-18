"""Тесты A2A inbound JSON-RPC server."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.bus import FleetBus, FleetMessage, MessageType
from src.http_server import create_app
from tests.test_miniapp_auth import FakeAgent


class FakeRuntime:
    def __init__(self, root: Path, agents: dict, bus: FleetBus):
        self.root = root
        self.agents = agents
        self.bus = bus

    def running_agents(self) -> list[str]:
        return list(self.agents)


@pytest.fixture
def bus_fleet(tmp_path: Path):
    bus = FleetBus()
    root = tmp_path
    (root / "agents").mkdir()
    me_dir = root / "agents" / "me"
    (me_dir / "skills").mkdir(parents=True)
    me = FakeAgent("me", "ME_TOKEN", [1001], master=True)
    me.agent_dir = str(me_dir)
    me.config = {"name": "me", "display_name": "Me", "description": "Master"}
    runtime = FakeRuntime(root, {"me": me}, bus)
    app = create_app(runtime, bus=bus)
    return bus, runtime, TestClient(app)


def _envelope(text: str, req_id: str = "1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": uuid.uuid4().hex,
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


def _allow_unauth(monkeypatch):
    monkeypatch.setenv("A2A_INBOUND_ALLOW_UNAUTH", "1")
    monkeypatch.delenv("A2A_INBOUND_TOKEN", raising=False)


async def _auto_reply_worker(
    bus: FleetBus, reply_text: str = "hello back", ready: asyncio.Event | None = None,
):
    """Эмулирует worker: подхватывает сообщение для agent:me и отвечает в reply_to."""
    q = bus.subscribe("agent:me")
    if ready is not None:
        ready.set()
    msg = await q.get()
    reply_to = msg.metadata.get("reply_to")
    if reply_to:
        await bus.publish(
            FleetMessage(
                source="agent:me",
                target=reply_to,
                content=reply_text,
                msg_type=MessageType.AGENT_TO_AGENT,
                metadata={"delegation_id": msg.metadata.get("delegation_id", "")},
            )
        )
    return msg


# ── Auth ───────────────────────────────────────────────────────────────────


class TestInboundAuth:
    def test_no_token_no_allow_rejects(self, bus_fleet, monkeypatch):
        monkeypatch.delenv("A2A_INBOUND_TOKEN", raising=False)
        monkeypatch.delenv("A2A_INBOUND_ALLOW_UNAUTH", raising=False)
        _, _, client = bus_fleet
        r = client.post("/a2a/me", json=_envelope("hi"))
        assert r.status_code == 403

    def test_token_required_when_set(self, bus_fleet, monkeypatch):
        monkeypatch.setenv("A2A_INBOUND_TOKEN", "secret")
        _, _, client = bus_fleet
        r = client.post("/a2a/me", json=_envelope("hi"))
        assert r.status_code == 401
        r2 = client.post(
            "/a2a/me",
            json=_envelope("hi"),
            headers={"Authorization": "Bearer wrong"},
        )
        assert r2.status_code == 401


# ── JSON-RPC semantics ─────────────────────────────────────────────────────


class TestJsonRpc:
    def test_invalid_json_returns_parse_error(self, bus_fleet, monkeypatch):
        _allow_unauth(monkeypatch)
        _, _, client = bus_fleet
        r = client.post("/a2a/me", data="not-json", headers={"Content-Type": "application/json"})
        body = r.json()
        assert body["error"]["code"] == -32700

    def test_missing_jsonrpc_version(self, bus_fleet, monkeypatch):
        _allow_unauth(monkeypatch)
        _, _, client = bus_fleet
        r = client.post("/a2a/me", json={"id": 1, "method": "message/send", "params": {}})
        body = r.json()
        assert body["error"]["code"] == -32600

    def test_unknown_method(self, bus_fleet, monkeypatch):
        _allow_unauth(monkeypatch)
        _, _, client = bus_fleet
        r = client.post(
            "/a2a/me",
            json={"jsonrpc": "2.0", "id": 1, "method": "foo/bar", "params": {}},
        )
        body = r.json()
        assert body["error"]["code"] == -32601

    def test_missing_message_params(self, bus_fleet, monkeypatch):
        _allow_unauth(monkeypatch)
        _, _, client = bus_fleet
        r = client.post(
            "/a2a/me",
            json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}},
        )
        body = r.json()
        assert body["error"]["code"] == -32602


# ── End-to-end: bus roundtrip ──────────────────────────────────────────────


class TestMessageSend:
    def test_unknown_agent_invalid_params(self, bus_fleet, monkeypatch):
        _allow_unauth(monkeypatch)
        _, _, client = bus_fleet
        r = client.post("/a2a/ghost", json=_envelope("hi"))
        body = r.json()
        assert body["error"]["code"] == -32602

    def test_empty_text_rejected(self, bus_fleet, monkeypatch):
        _allow_unauth(monkeypatch)
        _, _, client = bus_fleet
        bad = {
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {
                "role": "user", "messageId": "abc",
                "parts": [{"kind": "text", "text": ""}],
            }}
        }
        r = client.post("/a2a/me", json=bad)
        body = r.json()
        assert body["error"]["code"] == -32602

    def test_timeout_on_no_reply(self, bus_fleet, monkeypatch):
        _allow_unauth(monkeypatch)
        bus, _, client = bus_fleet
        # Подписчик 'agent:me' есть (через subscribe внутри handler) но никто не отвечает.
        bus.subscribe("agent:me")  # чтобы delivered >= 1, не было warning.
        env = _envelope("hi")
        env["params"]["timeout"] = 0.5  # быстрый таймаут
        r = client.post("/a2a/me", json=env)
        body = r.json()
        assert body["error"]["code"] == -32603
        assert "did not reply" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_end_to_end_roundtrip(self, tmp_path, monkeypatch):
        """worker'а подставляем вручную, проверяем полный путь."""
        _allow_unauth(monkeypatch)

        # Создаём собственный stack (не фикстуру — нужен async event loop)
        import httpx
        from httpx import ASGITransport

        bus = FleetBus()
        root = tmp_path
        (root / "agents").mkdir()
        me_dir = root / "agents" / "me"
        (me_dir / "skills").mkdir(parents=True)
        me = FakeAgent("me", "ME_TOKEN", [1001], master=True)
        me.agent_dir = str(me_dir)
        me.config = {"name": "me", "display_name": "Me", "description": "x"}
        runtime = FakeRuntime(root, {"me": me}, bus)
        app = create_app(runtime, bus=bus)

        ready = asyncio.Event()
        worker = asyncio.create_task(
            _auto_reply_worker(bus, "reply text 42", ready=ready)
        )
        await ready.wait()

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r = await client.post("/a2a/me", json=_envelope("prompt"))

        consumed = await asyncio.wait_for(worker, timeout=5)

        assert r.status_code == 200
        body = r.json()
        assert "result" in body, body
        reply = body["result"]
        assert reply["role"] == "agent"
        texts = [p["text"] for p in reply["parts"] if p.get("kind") == "text"]
        assert texts == ["reply text 42"]
        assert consumed.metadata["source_role"] == "master"
        assert consumed.metadata["reply_to"].startswith("a2a:resp:")
        assert consumed.content == "prompt"
