"""Тесты /dashboard команды — выдача Mini App кнопки."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.telegram_bridge import BOT_COMMANDS, TelegramBridge, _commands_for


def test_commands_for_master_includes_dashboard():
    names = {c.command for c in _commands_for(is_master=True)}
    assert "dashboard" in names
    assert "setup_dashboard" in names
    assert len(_commands_for(is_master=True)) == len(BOT_COMMANDS)


def test_commands_for_non_master_hides_dashboard():
    names = {c.command for c in _commands_for(is_master=False)}
    assert "dashboard" not in names
    assert "setup_dashboard" not in names
    # Остальные команды остаются — например /help, /status.
    assert "help" in names
    assert "status" in names


class _Agent:
    name = "me"
    display_name = "Master"
    is_master = True


class _NonMasterAgent:
    name = "coder"
    display_name = "Coder"
    is_master = False


@pytest.fixture
def bridge():
    b = TelegramBridge.__new__(TelegramBridge)
    b.agent = _Agent()
    b._reply = AsyncMock()
    return b


@pytest.mark.asyncio
async def test_non_master_agent_refuses_dashboard():
    b = TelegramBridge.__new__(TelegramBridge)
    b.agent = _NonMasterAgent()
    b._reply = AsyncMock()
    update, msg = _make_update()
    await b._cmd_dashboard(update, None, "")
    b._reply.assert_awaited_once()
    text = b._reply.await_args.args[2]
    assert "master" in text.lower()
    msg.reply_text.assert_not_awaited()


def _make_update():
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = SimpleNamespace(effective_message=msg)
    return update, msg


@pytest.mark.asyncio
async def test_missing_miniapp_url_replies_with_hint(bridge, monkeypatch):
    monkeypatch.delenv("MINIAPP_URL", raising=False)
    update, _ = _make_update()
    await bridge._cmd_dashboard(update, None, "")
    bridge._reply.assert_awaited_once()
    text = bridge._reply.await_args.args[2]
    assert "MINIAPP_URL" in text


@pytest.mark.asyncio
async def test_http_url_rejected(bridge, monkeypatch):
    monkeypatch.setenv("MINIAPP_URL", "http://example.com/miniapp")
    update, _ = _make_update()
    await bridge._cmd_dashboard(update, None, "")
    bridge._reply.assert_awaited_once()
    text = bridge._reply.await_args.args[2]
    assert "HTTPS" in text


@pytest.mark.asyncio
async def test_valid_url_sends_webapp_button(bridge, monkeypatch):
    monkeypatch.setenv("MINIAPP_URL", "https://example.com/miniapp")
    update, msg = _make_update()
    await bridge._cmd_dashboard(update, None, "")

    bridge._reply.assert_not_awaited()
    msg.reply_text.assert_awaited_once()

    call_kwargs = msg.reply_text.await_args.kwargs
    markup = call_kwargs["reply_markup"]
    # InlineKeyboardMarkup structure
    kb = markup.inline_keyboard
    assert len(kb) == 1 and len(kb[0]) == 1
    btn = kb[0][0]
    assert btn.web_app is not None
    assert btn.web_app.url.startswith("https://example.com/miniapp")
    assert "origin_agent=me" in btn.web_app.url


@pytest.mark.asyncio
async def test_origin_agent_appended_with_existing_query(bridge, monkeypatch):
    monkeypatch.setenv("MINIAPP_URL", "https://example.com/miniapp?v=1")
    update, msg = _make_update()
    await bridge._cmd_dashboard(update, None, "")
    btn = msg.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert btn.web_app.url == "https://example.com/miniapp?v=1&origin_agent=me"
