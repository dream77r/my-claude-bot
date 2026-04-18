"""Тесты A2A outbound client."""

from __future__ import annotations

import uuid

import httpx
import pytest

from src.a2a.client import (
    A2AClientError,
    A2ARemoteError,
    call_agent,
    fetch_agent_card,
)


# ── fetch_agent_card ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_agent_card_parses_spec(monkeypatch):
    """Карточка возвращённая сервером должна парситься в AgentCard."""
    card_payload = {
        "protocolVersion": "0.3.0",
        "name": "remote-me",
        "description": "Remote",
        "url": "https://remote.example.com/a2a/me",
        "version": "1.0.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "provider": {
            "organization": "remote",
            "url": "https://remote.example.com",
        },
        "skills": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/agent-card/me"
        return httpx.Response(200, json=card_payload)

    transport = httpx.MockTransport(handler)
    import src.a2a.client as mod

    class StubClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=transport)

    monkeypatch.setattr(mod.httpx, "AsyncClient", StubClient)
    card = await fetch_agent_card("https://remote.example.com/.well-known/agent-card/me")
    assert card.name == "remote-me"
    assert card.url == "https://remote.example.com/a2a/me"


# ── call_agent ─────────────────────────────────────────────────────────────


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    import src.a2a.client as mod

    class StubClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=transport)

    monkeypatch.setattr(mod.httpx, "AsyncClient", StubClient)


@pytest.mark.asyncio
async def test_call_agent_success(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        captured["caller"] = request.headers.get("X-A2A-Caller")
        captured["auth"] = request.headers.get("Authorization")
        reply = {
            "jsonrpc": "2.0", "id": "x",
            "result": {
                "role": "agent",
                "messageId": uuid.uuid4().hex,
                "parts": [{"kind": "text", "text": "pong"}],
            },
        }
        return httpx.Response(200, json=reply)

    _patch_transport(monkeypatch, handler)
    resp = await call_agent(
        "https://remote.example.com/a2a/me",
        "ping",
        auth_token="secret",
        caller_name="me",
    )
    assert resp.text == "pong"
    assert captured["url"] == "https://remote.example.com/a2a/me"
    assert captured["caller"] == "me"
    assert captured["auth"] == "Bearer secret"


@pytest.mark.asyncio
async def test_call_agent_remote_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "x", "error": {"code": -32601, "message": "nope"}},
        )

    _patch_transport(monkeypatch, handler)
    with pytest.raises(A2ARemoteError) as exc:
        await call_agent("https://r.example.com/a2a/me", "ping")
    assert exc.value.code == -32601


@pytest.mark.asyncio
async def test_call_agent_http_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(A2AClientError, match="HTTP 500"):
        await call_agent("https://r.example.com/a2a/me", "ping")


@pytest.mark.asyncio
async def test_call_agent_invalid_result(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "x", "result": {"not": "a-message"}},
        )

    _patch_transport(monkeypatch, handler)
    with pytest.raises(A2AClientError, match="valid Message"):
        await call_agent("https://r.example.com/a2a/me", "ping")


@pytest.mark.asyncio
async def test_call_agent_sends_envelope_shape(monkeypatch):
    import json
    def handler(request: httpx.Request) -> httpx.Response:
        env = json.loads(request.read())
        assert env["jsonrpc"] == "2.0"
        assert env["method"] == "message/send"
        msg = env["params"]["message"]
        assert msg["role"] == "user"
        assert msg["parts"][0]["text"] == "hello"
        reply = {
            "jsonrpc": "2.0", "id": env["id"],
            "result": {
                "role": "agent",
                "messageId": uuid.uuid4().hex,
                "parts": [{"kind": "text", "text": "ok"}],
            },
        }
        return httpx.Response(200, json=reply)

    _patch_transport(monkeypatch, handler)
    resp = await call_agent("https://r.example.com/a2a/me", "hello")
    assert resp.text == "ok"
