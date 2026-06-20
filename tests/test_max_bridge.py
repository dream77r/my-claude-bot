"""Тесты для max_bridge.py — мост в мессенджер МАКС (второй транспорт).

maxapi в тестовой среде не установлен; MaxBridge держит его импорт внутри run(),
поэтому модуль импортируется и тестируется без SDK. Покрываем чистую логику:
извлечение полей события, контроль доступа, публикацию inbound с transport=max,
и outbound-диспетчеризацию (только OUTBOUND, файлы → пометка + clear_outbox).
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bus import FleetBus, FleetMessage, MessageType
from src.max_bridge import (
    MaxBridge,
    _MaxStatus,
    _dig,
    _extract_mid,
    _split_text,
    _upload_type_for,
)


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

    def test_upload_type_by_extension(self):
        assert _upload_type_for("photo.JPG") == "image"
        assert _upload_type_for("/a/b/clip.mp4") == "video"
        assert _upload_type_for("voice.ogg") == "audio"
        assert _upload_type_for("report.pdf") == "file"
        assert _upload_type_for("archive.zip") == "file"
        assert _upload_type_for("noext") == "file"

    def test_extract_mid_from_sended_message(self):
        sent = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="mid-42"))
        )
        assert _extract_mid(sent) == "mid-42"

    def test_extract_mid_missing_returns_none(self):
        assert _extract_mid(SimpleNamespace()) is None


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
    async def test_files_fallback_note_and_clear_outbox(self, bridge):
        # Детерминированный fallback: maxapi нет (_InputMedia=None) → реальную
        # загрузку не делаем, шлём текстовую пометку, но outbox всё равно чистим.
        # (Раньше тест проходил «по случайности» — upload_media на MagicMock не
        # awaitable, падал и уходил в fallback; теперь явно фиксируем путь.)
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.upload_media = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="вот файл",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            files=["/tmp/agents/test/memory/outbox/report.pdf"],
            metadata={"event": "response", "agent_dir": "/tmp/agents/test"},
        )
        with patch("src.max_bridge.memory.log_message"), \
                patch("src.max_bridge._InputMedia", None), \
                patch("src.max_bridge.clear_outbox") as clear:
            await bridge._dispatch_outbound(msg)
        bridge._bot.upload_media.assert_not_awaited()  # SDK нет → не грузим
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


# ── Индикаторы «прочитано / печатает…» (отзывчивость) ──

_FAKE_ACTION = SimpleNamespace(TYPING_ON="typing_on", MARK_SEEN="mark_seen")


class TestTypingIndicator:
    @pytest.mark.asyncio
    async def test_processing_started_seen_and_starts_typing_loop(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_action = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="",
            msg_type=MessageType.SYSTEM, chat_id=555,
            metadata={"event": "processing_started"},
        )
        with patch("src.max_bridge._SenderAction", _FAKE_ACTION):
            await bridge._dispatch_outbound(msg)
            # read-receipt отправлен синхронно
            seen = [c.kwargs["action"] for c in bridge._bot.send_action.await_args_list]
            assert "mark_seen" in seen
            # фоновый таймер «печатает…» запущен для этого чата
            assert 555 in bridge._typing_tasks
            # дать таймеру один тик — он шлёт TYPING_ON
            await asyncio.sleep(0.01)
            actions = [c.kwargs["action"] for c in bridge._bot.send_action.await_args_list]
            assert "typing_on" in actions
            bridge._stop_typing(555)  # cleanup

    @pytest.mark.asyncio
    async def test_response_stops_typing_loop(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_action = AsyncMock()
        bridge._bot.send_message = AsyncMock()
        with patch("src.max_bridge._SenderAction", _FAKE_ACTION), \
                patch("src.max_bridge.memory.log_message"):
            bridge._start_typing(555)
            assert 555 in bridge._typing_tasks
            resp = FleetMessage(
                source="agent:test", target="max:test", content="готово",
                msg_type=MessageType.OUTBOUND, chat_id=555,
                metadata={"event": "response"},
            )
            await bridge._dispatch_outbound(resp)
        # финальный ответ погасил таймер
        assert 555 not in bridge._typing_tasks

    @pytest.mark.asyncio
    async def test_processing_started_sends_status_not_raw_content(self, bridge):
        # processing_started заводит живой статус «⏳ Думаю…» — но НЕ выливает
        # сырой SYSTEM-контент ("служебное") в чат.
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.send_action = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="служебное",
            msg_type=MessageType.SYSTEM, chat_id=555,
            metadata={"event": "processing_started"},
        )
        with patch("src.max_bridge._SenderAction", _FAKE_ACTION):
            await bridge._dispatch_outbound(msg)
            bridge._stop_typing(555)
        # Отправлено ровно стартовое статус-сообщение, без сырого контента.
        bridge._bot.send_message.assert_awaited_once()
        sent_text = bridge._bot.send_message.await_args.kwargs["text"]
        assert "Думаю" in sent_text
        assert "служебное" not in sent_text

    @pytest.mark.asyncio
    async def test_no_typing_when_sdk_absent(self, bridge):
        # _SenderAction=None (maxapi не установлен) → таймер не стартует, без падений.
        bridge._bot = MagicMock()
        bridge._bot.send_action = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="",
            msg_type=MessageType.SYSTEM, chat_id=555,
            metadata={"event": "processing_started"},
        )
        with patch("src.max_bridge._SenderAction", None):
            await bridge._dispatch_outbound(msg)
        assert 555 not in bridge._typing_tasks
        bridge._bot.send_action.assert_not_awaited()


# ── Задача A: реальная отправка файлов-вложений ──


class _FakeInputMedia:
    """Заглушка maxapi.types.InputMedia: запоминает path/type, файл не читает."""

    def __init__(self, path, type=None):  # noqa: A002 — зеркалит сигнатуру SDK
        self.path = path
        self.type = type


class TestFileUpload:
    @pytest.mark.asyncio
    async def test_real_attachment_uploaded_and_sent(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.upload_media = AsyncMock(return_value="ATTACH_TOKEN")
        msg = FleetMessage(
            source="agent:test", target="max:test", content="вот картинка",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            files=["/tmp/agents/test/memory/outbox/pic.png"],
            metadata={"event": "response", "agent_dir": "/tmp/agents/test"},
        )
        with patch("src.max_bridge.memory.log_message"), \
                patch("src.max_bridge._InputMedia", _FakeInputMedia), \
                patch("src.max_bridge.clear_outbox") as clear:
            await bridge._dispatch_outbound(msg)
        # upload_media получил InputMedia с типом image (по расширению .png)
        media = bridge._bot.upload_media.await_args.args[0]
        assert isinstance(media, _FakeInputMedia)
        assert media.type == "image"
        # Реальное вложение ушло через attachments (а не текстовой пометкой)
        attach_calls = [
            c for c in bridge._bot.send_message.await_args_list
            if c.kwargs.get("attachments")
        ]
        assert len(attach_calls) == 1
        assert attach_calls[0].kwargs["attachments"] == ["ATTACH_TOKEN"]
        clear.assert_called_once_with("/tmp/agents/test")

    @pytest.mark.asyncio
    async def test_image_upload_retries_as_file(self, bridge):
        # Фото-сервер МАКС не принял картинку (1-я попытка type=image падает) →
        # ретрай как документ (type=file) проходит, вложение всё равно уходит.
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.upload_media = AsyncMock(
            side_effect=[RuntimeError("no photos"), "FILE_TOKEN"]
        )
        with patch("src.max_bridge._InputMedia", _FakeInputMedia):
            await bridge._send_files(555, ["/tmp/x/pic.png"])
        # Две попытки upload_media: image, затем file
        types = [c.args[0].type for c in bridge._bot.upload_media.await_args_list]
        assert types == ["image", "file"]
        # Вложение ушло (через ретрай), без текстовой пометки
        attach_calls = [
            c for c in bridge._bot.send_message.await_args_list
            if c.kwargs.get("attachments") == ["FILE_TOKEN"]
        ]
        assert len(attach_calls) == 1

    @pytest.mark.asyncio
    async def test_fallback_text_note_when_sdk_absent(self, bridge):
        # _InputMedia=None (maxapi нет) → текстовая пометка + clear_outbox.
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.upload_media = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="файл",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            files=["/tmp/agents/test/memory/outbox/report.pdf"],
            metadata={"event": "response", "agent_dir": "/tmp/agents/test"},
        )
        with patch("src.max_bridge.memory.log_message"), \
                patch("src.max_bridge._InputMedia", None), \
                patch("src.max_bridge.clear_outbox") as clear:
            await bridge._dispatch_outbound(msg)
        bridge._bot.upload_media.assert_not_awaited()
        note = bridge._bot.send_message.await_args.kwargs["text"]
        assert "report.pdf" in note
        clear.assert_called_once_with("/tmp/agents/test")

    @pytest.mark.asyncio
    async def test_fallback_when_upload_fails(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.upload_media = AsyncMock(side_effect=RuntimeError("upload boom"))
        with patch("src.max_bridge._InputMedia", _FakeInputMedia):
            await bridge._send_files(555, ["/tmp/x/clip.mp4"])
        # upload упал → нет attachments-отправки, но есть текстовая пометка
        attach_calls = [
            c for c in bridge._bot.send_message.await_args_list
            if c.kwargs.get("attachments")
        ]
        assert not attach_calls
        note = bridge._bot.send_message.await_args.kwargs["text"]
        assert "clip.mp4" in note

    @pytest.mark.asyncio
    async def test_real_upload_failure_still_clears_outbox_e2e(self, bridge):
        # Сквозь _dispatch_outbound: реальный _send_files + upload_media падает →
        # текстовая пометка И outbox очищен (инвариант на настоящем failure-пути).
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.upload_media = AsyncMock(side_effect=RuntimeError("upload boom"))
        msg = FleetMessage(
            source="agent:test", target="max:test", content="",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            files=["/tmp/agents/test/memory/outbox/clip.mp4"],
            metadata={"event": "response", "agent_dir": "/tmp/agents/test"},
        )
        with patch("src.max_bridge.memory.log_message"), \
                patch("src.max_bridge._InputMedia", _FakeInputMedia), \
                patch("src.max_bridge.clear_outbox") as clear:
            await bridge._dispatch_outbound(msg)
        # Нет attachments-отправки (upload упал), но есть текстовая пометка…
        attach_calls = [
            c for c in bridge._bot.send_message.await_args_list
            if c.kwargs.get("attachments")
        ]
        assert not attach_calls
        assert "clip.mp4" in bridge._bot.send_message.await_args.kwargs["text"]
        # …и outbox всё равно очищен.
        clear.assert_called_once_with("/tmp/agents/test")

    @pytest.mark.asyncio
    async def test_outbox_cleared_even_if_send_files_raises(self, bridge):
        # Инвариант: outbox чистится даже если отправка файлов бросила исключение.
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            files=["/tmp/agents/test/memory/outbox/a.pdf"],
            metadata={"event": "response", "agent_dir": "/tmp/agents/test"},
        )
        with patch("src.max_bridge.memory.log_message"), \
                patch.object(bridge, "_send_files",
                             AsyncMock(side_effect=RuntimeError("boom"))), \
                patch("src.max_bridge.clear_outbox") as clear:
            with pytest.raises(RuntimeError):
                await bridge._dispatch_outbound(msg)
        clear.assert_called_once_with("/tmp/agents/test")


# ── _MaxStatus: юнит-тесты живого статус-сообщения ──


def _sended(mid="mid-1"):
    """Сэмулировать SendedMessage с body.mid."""
    return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid=mid)))


class TestMaxStatus:
    @pytest.mark.asyncio
    async def test_start_success_stores_mid(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=_sended("mid-7"))
        st = _MaxStatus(bot, 555)
        ok = await st.start("⏳ Думаю…")
        assert ok is True
        assert st.message_id == "mid-7"

    @pytest.mark.asyncio
    async def test_start_failure_returns_false(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("down"))
        st = _MaxStatus(bot, 555)
        assert await st.start("⏳") is False
        assert st.message_id is None

    @pytest.mark.asyncio
    async def test_start_no_mid_returns_false(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=SimpleNamespace())  # нет body.mid
        st = _MaxStatus(bot, 555)
        assert await st.start("⏳") is False

    @pytest.mark.asyncio
    async def test_show_throttle_skips_within_interval(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        st = _MaxStatus(bot, 555, throttle=1.2)
        st.message_id = "mid-1"
        st._last_edit = time.monotonic()  # только что → внутри интервала
        await st.show("новый текст")
        bot.edit_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_show_edits_after_interval(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        st = _MaxStatus(bot, 555, throttle=1.2)
        st.message_id = "mid-1"
        st._last_edit = 0.0  # давно → интервал прошёл
        await st.show("живой текст")
        bot.edit_message.assert_awaited_once()
        assert bot.edit_message.await_args.kwargs["text"] == "живой текст"

    @pytest.mark.asyncio
    async def test_show_noop_without_message_id(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        st = _MaxStatus(bot, 555)
        await st.show("текст")  # message_id ещё None
        bot.edit_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_finalize_short_edits_in_place(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        st = _MaxStatus(bot, 555)
        st.message_id = "mid-1"
        remainder = await st.finalize("финальный ответ")
        bot.edit_message.assert_awaited_once()
        assert bot.edit_message.await_args.kwargs["text"] == "финальный ответ"
        assert remainder == []

    @pytest.mark.asyncio
    async def test_finalize_long_returns_remainder(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        st = _MaxStatus(bot, 555, limit=4000)
        st.message_id = "mid-1"
        remainder = await st.finalize("z" * 9000)
        # Первый кусок ушёл в edit, хвост (2 куска) — на досылку
        assert bot.edit_message.await_count == 1
        assert len(remainder) == 2

    @pytest.mark.asyncio
    async def test_finalize_without_message_id_returns_full(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        st = _MaxStatus(bot, 555)  # message_id None
        remainder = await st.finalize("ответ")
        bot.edit_message.assert_not_awaited()
        assert remainder == ["ответ"]

    @pytest.mark.asyncio
    async def test_finalize_edit_failure_returns_full(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock(side_effect=RuntimeError("edit down"))
        st = _MaxStatus(bot, 555)
        st.message_id = "mid-1"
        remainder = await st.finalize("ответ")
        # edit упал → весь текст возвращается на досылку новыми сообщениями
        assert remainder == ["ответ"]

    @pytest.mark.asyncio
    async def test_finalize_skips_identical_edit(self):
        # Короткий ответ уже отстримился через show() → finalize НЕ должен слать
        # identical-edit (иначе «not modified» → дубликат ответа новым сообщением).
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        st = _MaxStatus(bot, 555)
        st.message_id = "mid-1"
        st._last_text = "готовый ответ"  # show() уже показал этот текст
        remainder = await st.finalize("готовый ответ")
        bot.edit_message.assert_not_awaited()  # no-op edit пропущен
        assert remainder == []

    @pytest.mark.asyncio
    async def test_show_skips_identical_text(self):
        # Интервал прошёл, но текст не изменился → edit не шлём («not modified»).
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        st = _MaxStatus(bot, 555, throttle=1.2)
        st.message_id = "mid-1"
        st._last_edit = 0.0  # интервал прошёл
        st._last_text = "тот же текст"
        await st.show("тот же текст")
        bot.edit_message.assert_not_awaited()


# ── Задача B: живой стриминг ответа на уровне моста ──


class TestLiveStreaming:
    @pytest.mark.asyncio
    async def test_processing_started_creates_status(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock(return_value=_sended("mid-9"))
        bridge._bot.send_action = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="",
            msg_type=MessageType.SYSTEM, chat_id=555,
            metadata={"event": "processing_started"},
        )
        with patch("src.max_bridge._SenderAction", _FAKE_ACTION):
            await bridge._dispatch_outbound(msg)
            bridge._stop_typing(555)
        assert 555 in bridge._status_messages
        assert bridge._status_messages[555].message_id == "mid-9"

    @pytest.mark.asyncio
    async def test_text_delta_edits_status(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.edit_message = AsyncMock()
        # статус уже существует, интервал прошёл
        st = _MaxStatus(bridge._bot, 555, throttle=1.2)
        st.message_id = "mid-1"
        st._last_edit = 0.0
        bridge._status_messages[555] = st
        msg = FleetMessage(
            source="agent:test", target="max:test", content="накопленный ответ",
            msg_type=MessageType.SYSTEM, chat_id=555,
            metadata={"event": "text_delta"},
        )
        await bridge._dispatch_outbound(msg)
        bridge._bot.edit_message.assert_awaited_once()
        assert bridge._bot.edit_message.await_args.kwargs["text"] == "накопленный ответ"

    @pytest.mark.asyncio
    async def test_response_finalizes_status_in_place(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.edit_message = AsyncMock()
        st = _MaxStatus(bridge._bot, 555)
        st.message_id = "mid-1"
        bridge._status_messages[555] = st
        msg = FleetMessage(
            source="agent:test", target="max:test", content="итоговый ответ",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            metadata={"event": "response"},
        )
        with patch("src.max_bridge.memory.log_message"):
            await bridge._dispatch_outbound(msg)
        # Финал вписан edit'ом на месте, без нового send_message
        bridge._bot.edit_message.assert_awaited_once()
        assert bridge._bot.edit_message.await_args.kwargs["text"] == "итоговый ответ"
        bridge._bot.send_message.assert_not_awaited()
        assert 555 not in bridge._status_messages

    @pytest.mark.asyncio
    async def test_long_response_edits_first_then_sends_rest(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.edit_message = AsyncMock()
        st = _MaxStatus(bridge._bot, 555, limit=4000)
        st.message_id = "mid-1"
        bridge._status_messages[555] = st
        msg = FleetMessage(
            source="agent:test", target="max:test", content="z" * 9000,
            msg_type=MessageType.OUTBOUND, chat_id=555,
            metadata={"event": "response"},
        )
        with patch("src.max_bridge.memory.log_message"):
            await bridge._dispatch_outbound(msg)
        # Первый кусок — edit статуса, остальные 2 — новыми сообщениями
        bridge._bot.edit_message.assert_awaited_once()
        assert bridge._bot.send_message.await_count == 2

    @pytest.mark.asyncio
    async def test_error_finalizes_status_with_error_text(self, bridge):
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.edit_message = AsyncMock()
        st = _MaxStatus(bridge._bot, 555)
        st.message_id = "mid-1"
        bridge._status_messages[555] = st
        msg = FleetMessage(
            source="agent:test", target="max:test", content="⚠️ Таймаут",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            metadata={"event": "error", "error": "timeout"},
        )
        with patch("src.max_bridge.memory.log_message"):
            await bridge._dispatch_outbound(msg)
        bridge._bot.edit_message.assert_awaited_once()
        assert "Таймаут" in bridge._bot.edit_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_response_without_status_sends_new_message(self, bridge):
        # Старт статуса не удался → деградация к обычному финальному сообщению.
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock()
        bridge._bot.edit_message = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="ответ без статуса",
            msg_type=MessageType.OUTBOUND, chat_id=555,
            metadata={"event": "response"},
        )
        with patch("src.max_bridge.memory.log_message"):
            await bridge._dispatch_outbound(msg)
        bridge._bot.edit_message.assert_not_awaited()
        bridge._bot.send_message.assert_awaited_once()
        assert bridge._bot.send_message.await_args.kwargs["text"] == "ответ без статуса"

    @pytest.mark.asyncio
    async def test_stale_status_cleaned_on_next_processing_started(self, bridge):
        # Висячий статус прошлого хода (ход отменили, OUTBOUND не пришёл) →
        # новый processing_started финализирует старый и заводит свежий.
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock(return_value=_sended("mid-new"))
        bridge._bot.edit_message = AsyncMock()
        bridge._bot.send_action = AsyncMock()
        stale = _MaxStatus(bridge._bot, 555)
        stale.message_id = "mid-old"
        bridge._status_messages[555] = stale
        msg = FleetMessage(
            source="agent:test", target="max:test", content="",
            msg_type=MessageType.SYSTEM, chat_id=555,
            metadata={"event": "processing_started"},
        )
        with patch("src.max_bridge._SenderAction", _FAKE_ACTION):
            await bridge._dispatch_outbound(msg)
            bridge._stop_typing(555)
        # старый статус финализирован (edit «прервано»), новый — на месте
        bridge._bot.edit_message.assert_awaited_once()
        assert "прервано" in bridge._bot.edit_message.await_args.kwargs["text"]
        assert bridge._status_messages[555].message_id == "mid-new"

    @pytest.mark.asyncio
    async def test_begin_status_failure_no_crash(self, bridge):
        # send_message стартового статуса упал → статус не заводится, без падения.
        bridge._bot = MagicMock()
        bridge._bot.send_message = AsyncMock(side_effect=RuntimeError("down"))
        bridge._bot.send_action = AsyncMock()
        msg = FleetMessage(
            source="agent:test", target="max:test", content="",
            msg_type=MessageType.SYSTEM, chat_id=555,
            metadata={"event": "processing_started"},
        )
        with patch("src.max_bridge._SenderAction", _FAKE_ACTION):
            await bridge._dispatch_outbound(msg)
            bridge._stop_typing(555)
        assert 555 not in bridge._status_messages
