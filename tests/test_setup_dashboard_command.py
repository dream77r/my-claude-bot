"""Tests for /setup_dashboard — branching before the privileged helper runs."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.telegram_bridge import TelegramBridge


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


def _update_and_ctx():
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = SimpleNamespace(effective_message=msg, message=msg)
    ctx = SimpleNamespace(bot=MagicMock(set_chat_menu_button=AsyncMock()))
    return update, ctx


@pytest.mark.asyncio
async def test_non_master_rejected():
    b = TelegramBridge.__new__(TelegramBridge)
    b.agent = _NonMasterAgent()
    b._reply = AsyncMock()
    update, ctx = _update_and_ctx()
    await b._cmd_setup_dashboard(update, ctx, "bot.example.com")
    b._reply.assert_awaited()
    text = b._reply.await_args.args[2]
    assert "master" in text.lower()


@pytest.mark.asyncio
async def test_no_args_shows_usage(bridge):
    update, ctx = _update_and_ctx()
    await bridge._cmd_setup_dashboard(update, ctx, "")
    bridge._reply.assert_awaited_once()
    text = bridge._reply.await_args.args[2]
    assert "/setup_dashboard" in text
    assert "<domain>" in text


@pytest.mark.asyncio
async def test_bad_domain_rejected(bridge):
    update, ctx = _update_and_ctx()
    await bridge._cmd_setup_dashboard(update, ctx, "not a domain")
    bridge._reply.assert_awaited_once()
    text = bridge._reply.await_args.args[2]
    assert "домен" in text.lower() or "domain" in text.lower()


@pytest.mark.asyncio
async def test_bad_email_rejected(bridge):
    update, ctx = _update_and_ctx()
    await bridge._cmd_setup_dashboard(
        update, ctx, "bot.example.com not-an-email"
    )
    bridge._reply.assert_awaited_once()
    text = bridge._reply.await_args.args[2]
    assert "email" in text.lower()


@pytest.mark.asyncio
async def test_missing_sudoers_instructs_enable(bridge, monkeypatch):
    from src.miniapp import setup_flow

    async def _no(): return False
    monkeypatch.setattr(setup_flow, "sudoers_ready", _no)

    update, ctx = _update_and_ctx()
    await bridge._cmd_setup_dashboard(update, ctx, "bot.example.com")
    bridge._reply.assert_awaited_once()
    text = bridge._reply.await_args.args[2]
    assert "--enable-dashboard" in text


@pytest.mark.asyncio
async def test_happy_path_updates_env_and_sets_menu(
    bridge, monkeypatch, tmp_path
):
    from src.miniapp import setup_flow

    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=value\n", encoding="utf-8")

    restart_called = {"v": False}

    async def _yes(): return True
    async def _dns(d): return "203.0.113.5"
    async def _helper(domain, port, email=None):
        return True, "[setup-dashboard] done\n"
    async def _restart(unit="my-claude-bot"):
        restart_called["v"] = True

    monkeypatch.setattr(setup_flow, "sudoers_ready", _yes)
    monkeypatch.setattr(setup_flow, "check_dns", _dns)
    monkeypatch.setattr(setup_flow, "pick_free_port", lambda **_: 8091)
    monkeypatch.setattr(setup_flow, "run_setup_helper", _helper)
    monkeypatch.setattr(setup_flow, "trigger_restart_detached", _restart)
    monkeypatch.setattr(setup_flow, "ENV_PATH", env_file)

    update, ctx = _update_and_ctx()
    await bridge._cmd_setup_dashboard(update, ctx, "bot.example.com")

    # .env should now have our keys + preserved original.
    text = env_file.read_text(encoding="utf-8")
    assert "OTHER=value" in text
    assert "HTTP_PORT=8091" in text
    assert "PUBLIC_BASE_URL=https://bot.example.com" in text
    assert "MINIAPP_URL=https://bot.example.com/miniapp/" in text

    # Menu button set globally (no chat_id).
    ctx.bot.set_chat_menu_button.assert_awaited_once()
    kwargs = ctx.bot.set_chat_menu_button.await_args.kwargs
    assert "menu_button" in kwargs
    mb = kwargs["menu_button"]
    assert mb.web_app.url.endswith("origin_agent=me")

    # Restart scheduled.
    assert restart_called["v"] is True


@pytest.mark.asyncio
async def test_helper_failure_reports_tail(bridge, monkeypatch):
    from src.miniapp import setup_flow

    async def _yes(): return True
    async def _dns(d): return "203.0.113.5"
    async def _helper_fail(domain, port, email=None):
        return False, "line1\nline2\nERROR: certbot failed\n"

    monkeypatch.setattr(setup_flow, "sudoers_ready", _yes)
    monkeypatch.setattr(setup_flow, "check_dns", _dns)
    monkeypatch.setattr(setup_flow, "pick_free_port", lambda **_: 8092)
    monkeypatch.setattr(setup_flow, "run_setup_helper", _helper_fail)

    update, ctx = _update_and_ctx()
    await bridge._cmd_setup_dashboard(update, ctx, "bot.example.com")

    texts = [call.args[2] for call in bridge._reply.await_args_list]
    assert any("certbot failed" in t for t in texts)
    # Menu button NOT set when helper fails.
    ctx.bot.set_chat_menu_button.assert_not_awaited()
