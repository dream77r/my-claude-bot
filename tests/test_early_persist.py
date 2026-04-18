"""Тесты раннего persist входящих сообщений (до буферизации/LLM)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.telegram_bridge import TelegramBridge


class _Agent:
    name = "me"
    display_name = "Master"
    is_master = True
    is_multi_user = False
    agent_dir = "agents/me"
    allowed_users = [42]

    def get_effective_dir(self, uid):
        return self.agent_dir


def _bridge():
    b = TelegramBridge.__new__(TelegramBridge)
    b.agent = _Agent()
    b._buffers = {}
    b._user_ids = {}
    b._thread_ids = {}
    b._wizard_state = {}
    b._pending_group_setups = {}
    b._pending_topic_setups = {}
    return b


def _dm_update(text: str = "привет"):
    user = SimpleNamespace(id=42, first_name="Test", username="test")
    chat = SimpleNamespace(id=42, type="private")
    msg = MagicMock()
    msg.text = text
    msg.message_thread_id = None
    msg.entities = None
    msg.reply_to_message = None
    msg.reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=user, effective_chat=chat, message=msg
    )
    ctx = SimpleNamespace(bot=MagicMock(id=1, username="bot"))
    return update, ctx


@pytest.mark.asyncio
async def test_dm_text_persisted_before_buffer():
    """Сообщение логируется раньше чем попадает в буфер."""
    bridge = _bridge()
    bridge._check_auth = lambda u: True
    bridge._add_to_buffer = MagicMock()

    update, ctx = _dm_update("привет")

    call_order = []

    def track_log(*a, **kw):
        call_order.append(("log", a[1], a[2]))

    def track_buffer(*a, **kw):
        call_order.append(("buffer", a[1]))

    bridge._add_to_buffer.side_effect = track_buffer

    with patch("src.telegram_bridge.memory.log_message", side_effect=track_log) as log:
        await bridge._handle_text(update, ctx)

    # log_message был вызван с ролью "user" и правильным текстом
    log.assert_called_once()
    args = log.call_args.args
    assert args[1] == "user"
    assert args[2] == "привет"

    # Порядок: сначала log, потом buffer
    assert call_order[0][0] == "log"
    assert call_order[1][0] == "buffer"


@pytest.mark.asyncio
async def test_persist_survives_buffer_failure():
    """Если _add_to_buffer упал — сообщение уже на диске."""
    bridge = _bridge()
    bridge._check_auth = lambda u: True
    bridge._add_to_buffer = MagicMock(side_effect=RuntimeError("sim crash"))

    update, ctx = _dm_update("важное")

    with patch("src.telegram_bridge.memory.log_message") as log:
        with pytest.raises(RuntimeError):
            await bridge._handle_text(update, ctx)

    # log_message успел сработать до падения буфера
    log.assert_called_once()
    assert log.call_args.args[2] == "важное"


@pytest.mark.asyncio
async def test_group_does_not_double_log():
    """В группах уже есть log_group_message, log_message НЕ вызывается."""
    bridge = _bridge()
    bridge._check_auth = lambda u: True
    bridge._add_to_buffer = MagicMock()
    bridge._is_bot_mentioned = lambda u, c: True
    bridge._is_topic_allowed = lambda cid, tid: True
    bridge._get_sender_name = lambda u: "Sender"

    user = SimpleNamespace(id=42, first_name="Test", username="test")
    chat = SimpleNamespace(id=-100, type="group")
    msg = MagicMock()
    msg.text = "@bot привет"
    msg.message_thread_id = None
    msg.entities = None
    msg.reply_to_message = None
    msg.reply_text = AsyncMock()
    update = SimpleNamespace(effective_user=user, effective_chat=chat, message=msg)
    ctx = SimpleNamespace(bot=MagicMock(id=1, username="bot"))

    with patch("src.telegram_bridge.memory.log_message") as log, \
         patch("src.telegram_bridge.memory.log_group_message") as glog, \
         patch("src.telegram_bridge.memory.is_group_onboarding_needed", return_value=False):
        await bridge._handle_text(update, ctx)

    # В группах early persist идёт через log_group_message, не log_message
    log.assert_not_called()
    glog.assert_called_once()


@pytest.mark.asyncio
async def test_log_failure_does_not_block_buffer():
    """Если log_message падает — сообщение всё равно попадает в буфер."""
    bridge = _bridge()
    bridge._check_auth = lambda u: True
    bridge._add_to_buffer = MagicMock()

    update, ctx = _dm_update("текст")

    with patch(
        "src.telegram_bridge.memory.log_message",
        side_effect=OSError("disk full"),
    ):
        await bridge._handle_text(update, ctx)

    bridge._add_to_buffer.assert_called_once()
