"""Тесты /backend команды — глобальное переключение LLM-бэкенда."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.llm.manager import BackendConfig
from src.llm.registry import BACKENDS
from src.telegram_bridge import BOT_COMMANDS, TelegramBridge


class _Agent:
    name = "me"
    display_name = "Master"
    is_master = True
    is_multi_user = False
    backend = None
    claude_model = "sonnet"

    def is_user_allowed(self, user_id: int) -> bool:
        return True

    def get_effective_dir(self, user_id: int) -> str:
        return "/tmp/nonexistent"


@pytest.fixture
def bridge():
    b = TelegramBridge.__new__(TelegramBridge)
    b.agent = _Agent()
    b._reply = AsyncMock()
    return b


def _cmd_update():
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    return SimpleNamespace(
        message=msg,
        effective_chat=SimpleNamespace(id=1),
        effective_user=SimpleNamespace(id=42),
    )


def _callback_update(data: str):
    query = MagicMock()
    query.data = data
    query.from_user = SimpleNamespace(id=42)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return SimpleNamespace(callback_query=query), query


def test_bot_commands_includes_backend():
    names = {c.command for c in BOT_COMMANDS}
    assert "backend" in names


@pytest.mark.asyncio
async def test_cmd_backend_renders_keyboard_with_current_marked(bridge):
    update = _cmd_update()
    with patch(
        "src.llm.manager.load",
        lambda: BackendConfig(backend="codex"),
    ):
        await bridge._cmd_backend(update, None, "")

    update.message.reply_text.assert_awaited_once()
    call_kwargs = update.message.reply_text.await_args.kwargs
    kb = call_kwargs["reply_markup"].inline_keyboard

    # Одна кнопка на каждый бэкенд реестра
    assert len(kb) == len(BACKENDS)
    by_data = {row[0].callback_data: row[0].text for row in kb}
    assert set(by_data) == {f"backend:{bid}" for bid in BACKENDS}

    # Текущий бэкенд отмечен галочкой
    assert "✓" in by_data["backend:codex"]
    assert "✓" not in by_data["backend:claude"]


@pytest.mark.asyncio
async def test_backend_callback_shows_model_keyboard(bridge):
    update, query = _callback_update("backend:codex")

    await bridge._handle_callback(update, None)

    query.edit_message_text.assert_awaited_once()
    call_kwargs = query.edit_message_text.await_args.kwargs
    kb = call_kwargs["reply_markup"].inline_keyboard

    spec = BACKENDS["codex"]
    # Первая кнопка — дефолт платформы, дальше — все модели спека
    assert kb[0][0].callback_data == "backendmodel:codex:"
    assert "Дефолтная модель" in kb[0][0].text
    assert len(kb) == 1 + len(spec.models)
    for row, (model_id, _desc) in zip(kb[1:], spec.models.items()):
        assert row[0].callback_data == f"backendmodel:codex:{model_id}"


@pytest.mark.asyncio
async def test_backendmodel_callback_calls_set_global_default(bridge):
    update, query = _callback_update("backendmodel:codex:")

    with patch("src.llm.manager.set_global") as set_global:
        await bridge._handle_callback(update, None)

    set_global.assert_called_once_with("codex", None)
    text = query.edit_message_text.await_args.args[0]
    assert "OpenAI Codex" in text
    assert "по умолчанию" in text


@pytest.mark.asyncio
async def test_backendmodel_callback_calls_set_global_with_model(bridge):
    update, query = _callback_update("backendmodel:codex:gpt-5.1")

    with patch("src.llm.manager.set_global") as set_global:
        await bridge._handle_callback(update, None)

    set_global.assert_called_once_with("codex", "gpt-5.1")
    text = query.edit_message_text.await_args.args[0]
    assert "gpt-5.1" in text


@pytest.mark.asyncio
async def test_backend_callback_mimo_applies_immediately(bridge):
    """У mimo список моделей пуст — подтверждаем сразу без клавиатуры."""
    update, query = _callback_update("backend:mimo")

    with patch("src.llm.manager.set_global") as set_global:
        await bridge._handle_callback(update, None)

    set_global.assert_called_once_with("mimo", None)
    text = query.edit_message_text.await_args.args[0]
    assert "MiMo" in text
    assert "по умолчанию" in text
    assert query.edit_message_text.await_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_unknown_backend_ignored_gracefully(bridge):
    update, query = _callback_update("backend:nonexistent")

    with patch("src.llm.manager.set_global") as set_global:
        await bridge._handle_callback(update, None)

    set_global.assert_not_called()
    text = query.edit_message_text.await_args.args[0]
    assert "Неизвестный бэкенд" in text


@pytest.mark.asyncio
async def test_unknown_backendmodel_ignored_gracefully(bridge):
    update, query = _callback_update("backendmodel:nonexistent:")

    with patch("src.llm.manager.set_global") as set_global:
        await bridge._handle_callback(update, None)

    set_global.assert_not_called()
    text = query.edit_message_text.await_args.args[0]
    assert "Неизвестный бэкенд" in text
