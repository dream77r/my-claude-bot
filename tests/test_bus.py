"""Тесты для bus.py — MessageBus."""

import asyncio

import pytest

from src.bus import FleetBus, FleetMessage, MessageType


@pytest.fixture
def bus():
    return FleetBus()


class TestFleetMessage:
    def test_defaults(self):
        msg = FleetMessage(source="test", target="dest", content="hello")
        assert msg.source == "test"
        assert msg.target == "dest"
        assert msg.content == "hello"
        assert msg.msg_type == MessageType.INBOUND
        assert msg.files == []
        assert msg.metadata == {}
        assert len(msg.id) == 12
        assert msg.timestamp > 0

    def test_custom_fields(self):
        msg = FleetMessage(
            source="telegram",
            target="agent:me",
            content="привет",
            msg_type=MessageType.OUTBOUND,
            chat_id=12345,
            user_id=67890,
            files=["/tmp/test.pdf"],
            metadata={"key": "val"},
        )
        assert msg.chat_id == 12345
        assert msg.files == ["/tmp/test.pdf"]


class TestFleetBusSubscribe:
    def test_subscribe(self, bus):
        q = bus.subscribe("test")
        assert "test" in bus.subscribers
        assert isinstance(q, asyncio.Queue)

    def test_subscribe_idempotent(self, bus):
        q1 = bus.subscribe("test")
        q2 = bus.subscribe("test")
        assert q1 is q2

    def test_unsubscribe(self, bus):
        bus.subscribe("test")
        bus.unsubscribe("test")
        assert "test" not in bus.subscribers

    def test_unsubscribe_nonexistent(self, bus):
        bus.unsubscribe("nonexistent")  # не падает


class TestFleetBusPublish:
    @pytest.mark.asyncio
    async def test_direct_delivery(self, bus):
        q = bus.subscribe("agent:me")
        msg = FleetMessage(source="telegram", target="agent:me", content="hi")
        delivered = await bus.publish(msg)
        assert delivered == 1
        assert not q.empty()
        received = q.get_nowait()
        assert received.content == "hi"

    @pytest.mark.asyncio
    async def test_broadcast(self, bus):
        q1 = bus.subscribe("agent:a")
        q2 = bus.subscribe("agent:b")
        msg = FleetMessage(source="system", target="*", content="broadcast")
        delivered = await bus.publish(msg)
        assert delivered == 2
        assert q1.get_nowait().content == "broadcast"
        assert q2.get_nowait().content == "broadcast"

    @pytest.mark.asyncio
    async def test_prefix_delivery(self, bus):
        q1 = bus.subscribe("agent:a")
        q2 = bus.subscribe("agent:b")
        q3 = bus.subscribe("telegram")
        msg = FleetMessage(source="system", target="agent:*", content="agents only")
        delivered = await bus.publish(msg)
        assert delivered == 2
        assert q3.empty()

    @pytest.mark.asyncio
    async def test_no_recipients(self, bus):
        msg = FleetMessage(source="test", target="nobody", content="lost")
        delivered = await bus.publish(msg)
        assert delivered == 0

    @pytest.mark.asyncio
    async def test_queue_full(self, bus):
        bus.subscribe("test", maxsize=1)
        msg1 = FleetMessage(source="a", target="test", content="first")
        msg2 = FleetMessage(source="a", target="test", content="second")
        await bus.publish(msg1)
        delivered = await bus.publish(msg2)
        # Первый доставлен, второй потерян (очередь полна)
        assert delivered == 0


class TestFleetBusConsume:
    @pytest.mark.asyncio
    async def test_consume(self, bus):
        bus.subscribe("test")
        msg = FleetMessage(source="a", target="test", content="data")
        await bus.publish(msg)
        received = await bus.consume("test")
        assert received.content == "data"

    @pytest.mark.asyncio
    async def test_consume_unknown_subscriber(self, bus):
        with pytest.raises(KeyError):
            await bus.consume("unknown")


class TestFleetBusMatches:
    def test_exact_match(self):
        assert FleetBus._matches("agent:me", "agent:me") is True
        assert FleetBus._matches("agent:me", "agent:other") is False

    def test_broadcast(self):
        assert FleetBus._matches("anything", "*") is True

    def test_prefix_match(self):
        assert FleetBus._matches("agent:coder", "agent:*") is True
        assert FleetBus._matches("telegram", "agent:*") is False
