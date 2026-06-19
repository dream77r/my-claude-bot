"""Тесты для max_bridge.py — мост в мессенджер МАКС (второй транспорт).

maxapi в тестовой среде не установлен; MaxBridge держит его импорт внутри run(),
поэтому модуль импортируется и тестируется без SDK. Покрываем чистую логику:
извлечение полей события, контроль доступа, публикацию inbound с transport=max,
и outbound-диспетчеризацию (только OUTBOUND, файлы → пометка + clear_outbox).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bus import FleetBus, FleetMessage, MessageType
from src.max_bridge import MaxBridge, _dig, _split_text


def make_agent(name="test", allowed_users=None):
    agent = MagicMock()
    agent.name = name
    agent.agent_dir = f"/tmp/agents/{name}"
    agent.max_bot_token = "max-tok"
    agent.allowed_users = allowed_users or []
    agent.is_user_allowed = lambda uid: (
        not agent.allowed_users or uid in agent.allowed_users
    )
    return agent


def make_event(text="привет", chat_id=555, user_id=12345):
    """Сэмулировать структуру события maxapi: event.message.body.text и т.п."""
    sender = SimpleNamespace(user_id=user_id) if user_id is not None else SimpleNamespace()
    return SimpleNamespace(
        message=SimpleNamespace(
            body=SimpleNamespace(text=text),
            recipient=SimpleNamespace(chat_id=chat_id),
            sender=sender,
        )
    )


@pytest.fixture
def bus():
    return FleetBus()


@pytest.fixture
def bridge(bus):
    agent = make_agent()
    bus.subscribe("agent:test")
    return MaxBridge(agent, asyncio.Semaphore(1), bus=bus)


# ── Чистые хелперы ──

class TestHelpers:
    def test_dig_resolves_nested(self):
        obj = SimpleNamespace(a=SimpleNamespace(b=SimpleNamespace(c=7)))
        assert _dig(obj, "a.b.c") == 7

    def test_dig_first_match_wins(self):
        obj = SimpleNamespace(x=SimpleNamespace(t="hit"))
        assert _dig(obj, "missing.path", "x.t") == "hit"

    def test_dig_missing_returns_none(self):
        assert _dig(SimpleNamespace(), "nope.nada") is None

    def test_split_short_text_single(self):
        assert _split_text("короткий") == ["короткий"]

    def test_split_long_text_chunks(self):
        text = "a" * 9000
        parts = _split_text(text, limit=4000)
        assert len(parts) == 3
        assert all(len(p) <= 4000 for p in parts)
        assert "".join(parts) == text

    def test_split_prefers_newline(self):
        text = "x" * 3990 + "\n" + "y" * 100
        parts = _split_text(text, limit=4000)
        assert parts[0] == "x" * 3990
        assert parts[1] == "y" * 100


# ── Inbound: МАКС → bus ──

class TestInbound:
    @pytest.mark.asyncio
    async def test_publishes_with_transport_max(self, bridge, bus):
        with patch("src.max_bridge.memory.log_message"):
            await bridge._handle_incoming(make_event("эй", 555, 12345))
        msg = bus._queues["agent:test"].get_nowait()
        assert msg.content == "эй"
        assert msg.target == "agent:test"
        assert msg.chat_id == 555
        assert msg.user_id == 12345
        assert msg.msg_type == MessageType.INBOUND
        assert msg.metadata["transport"] == "max"
        assert msg.source == "max:555"

    @pytest.mark.asyncio
    async def test_empty_text_skipped(self, bridge, bus):
        with patch("src.max_bridge.memory.log_message"):
            await bridge._handle_incoming(make_event("   ", 555, 12345))
        assert bus._queues["agent:test"].empty()

    @pytest.mark.asyncio
    async def test_missing_chat_id_skipped(self, bridge, bus):
        broken = SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(text="hi")))
        with patch("src.max_bridge.memory.log_message"):
            await bridge._handle_incoming(broken)
        assert bus._queues["agent:test"].empty()

    @pytest.mark.asyncio
    async def test_disallowed_user_rejected(self, bus):
        agent = make_agent(allowed_users=[111])
        bus.subscribe("agent:test")
        br = MaxBridge(agent, asyncio.Semaphore(1), bus=bus)
        with patch("src.max_bridge.memory.log_message"):
            await br._handle_incoming(make_event("hi", 555, 222))  # 222 не в списке
        assert bus._queues["agent:test"].empty()

    @pytest.mark.asyncio
    async def test_unknown_sender_failclosed_when_restricted(self, bus):
        agent = make_agent(allowed_users=[111])
        bus.subscribe("agent:test")
        br = MaxBridge(agent, asyncio.Semaphore(1), bus=bus)
        with patch("src.max_bridge.memory.log_message"):
            await br._handle_incoming(make_event("hi", 555, user_id=None))
        # Отправителя не определить + список ограничен → fail-closed.
        assert bus._queues["agent:test"].empty()

    @pytest.mark.asyncio
    async def test_open_access_allows(self, bus):
        agent = make_agent(allowed_users=[])  # пустой список = всем можно
        bus.subscribe("agent:test")
        br = MaxBridge(agent, asyncio.Semaphore(1), bus=bus)
        with patch("src.max_bridge.memory.log_message"):
            await br._handle_incoming(make_event("hi", 555, user_id=None))
        assert not bus._queues["agent:test"].empty()


# ── Outbound: bus → МАКС ──

class TestOutbound:
    @pytest.mark.asyncio
    async def test_forwards_outbound_text(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="ответ",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            metadata={"event": "response"},
        )
        with patch("src.max_bridge.memory.log_message"):
            await bridge._dispatch_outbound(msg)
        bridge._bot.send_message.assert_awaited_once()
        kwargs = bridge._bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == 555
        assert kwargs["text"] == "ответ"

    @pytest.mark.asyncio
    async def test_ignores_system_streaming_events(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="печатает...",
            msg_type=MessageType.SYSTEM, chat_id=555,
            metadata={"event": "text_delta"},
        )
        await bridge._dispatch_outbound(msg)
        bridge._bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_outbound_without_chat_id_skipped(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="x",
            msg_type=MessageType.OUTBOUND, chat_id=0,
        )
        await bridge._dispatch_outbound(msg)
        bridge._bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_files_send_note_and_clear_outbox(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="вот файл",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            files=["/tmp/agents/test/memory/outbox/report.pdf"],
            metadata={"event": "response", "agent_dir": "/tmp/agents/test"},
        )
        with patch("src.max_bridge.memory.log_message"), \
                patch("src.max_bridge.clear_outbox") as clear:
            await bridge._dispatch_outbound(msg)
        # Текст ответа + пометка о файле = минимум 2 вызова send_message.
        assert bridge._bot.send_message.await_count >= 2
        note = bridge._bot.send_message.await_args.kwargs["text"]
        assert "report.pdf" in note
        clear.assert_called_once_with("/tmp/agents/test")

    @pytest.mark.asyncio
    async def test_long_reply_split_into_parts(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="z" * 9000,
            msg_type=MessageType.OUTBOUND, chat_id=555,
            metadata={"event": "response"},
        )
        with patch("src.max_bridge.memory.log_message"):
            await bridge._dispatch_outbound(msg)
        assert bridge._bot.send_message.await_count == 3
