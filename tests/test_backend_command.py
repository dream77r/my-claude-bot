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
    backend_model = None
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
    update, query = _callback_update("backendmodel:codex:gpt-6.1-sol")

    with patch("src.llm.manager.set_global") as set_global:
        await bridge._handle_callback(update, None)

    set_global.assert_called_once_with("codex", "gpt-6.1-sol")
    text = query.edit_message_text.await_args.args[0]
    assert "gpt-6.1-sol" in text


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


@pytest.mark.asyncio
@pytest.mark.parametrize("via_button", [False, True])
async def test_codex_model_selection_reaches_cli(bridge, tmp_path, via_button):
    from src import memory
    from src.llm import manager
    from src.llm.codex_cli import CodexCliBackend

    (tmp_path / "memory").mkdir()
    bridge.agent.agent_dir = str(tmp_path)
    bridge.agent.get_effective_dir = lambda uid: str(tmp_path)
    with patch("src.llm.manager.load", return_value=BackendConfig("codex")):
        if via_button:
            update, _ = _callback_update("model:gpt-6.1-sol")
            await bridge._handle_callback(update, None)
        else:
            await bridge._cmd_model(_cmd_update(), None, "gpt-6.1-sol")
        _, selected = manager.resolve(
            user_model=memory.get_setting(str(tmp_path), "backend_model"),
        )
    assert selected == "gpt-6.1-sol"

    async def fake_run_cli(**kwargs):
        argv = kwargs["argv"]
        assert argv[argv.index("--model") + 1] == selected
        assert argv.index("--ask-for-approval") < argv.index("exec")
        await kwargs["on_stdout_line"](
            '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}'
        )

    with patch("src.llm.codex_cli.resolve_cli_path", return_value="codex"), \
         patch("src.llm.codex_cli.run_cli", side_effect=fake_run_cli):
        result = await CodexCliBackend().chat(
            message="test", system_prompt="", cwd=str(tmp_path), model=selected,
        )
    assert result == "OK"


@pytest.mark.asyncio
async def test_model_menu_displays_effective_global_model(bridge, tmp_path):
    bridge.agent.agent_dir = str(tmp_path)
    update = _cmd_update()
    with patch("src.llm.manager.load", return_value=BackendConfig("codex", "gpt-6-astra")):
        await bridge._cmd_model(update, None, "")
    call = update.message.reply_text.await_args
    assert "GPT-6 Astra" in call.args[0]
    buttons = {
        b.callback_data: b.text
        for row in call.kwargs["reply_markup"].inline_keyboard for b in row
    }
    assert "✓" in buttons["model:gpt-6-astra"]
    assert "model:gpt-6.1-sol" in buttons


@pytest.mark.asyncio
async def test_model_command_rejects_other_backend_model(bridge, tmp_path):
    bridge.agent.agent_dir = str(tmp_path)
    with patch("src.llm.manager.load", return_value=BackendConfig("claude")), \
         patch("src.telegram_bridge.memory.set_setting") as save:
        await bridge._cmd_model(_cmd_update(), None, "gpt-6.1-sol")
    save.assert_not_called()
    assert "/backend" in bridge._reply.await_args.args[2]
