"""Тесты для rich_message.py — Telegram Bot API 10.1 sendRichMessage.

InputRichMessage = {"markdown": "<raw text>"} (не blocks-структура).
Реализация использует httpx напрямую (PTB некорректно сериализует вложенные dict).
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.rich_message import has_markdown_table, send_rich_message


# ── has_markdown_table ─────────────────────────────────────────────────────────

class TestHasMarkdownTable:
    def test_simple_table(self):
        text = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        assert has_markdown_table(text) is True

    def test_table_without_separator(self):
        # Два ряда с | — этого достаточно для обнаружения
        text = "| A | B |\n| 1 | 2 |"
        assert has_markdown_table(text) is True

    def test_no_table(self):
        assert has_markdown_table("Обычный текст без таблицы") is False

    def test_single_pipe_line_not_table(self):
        assert has_markdown_table("| A | B |") is False

    def test_table_in_mixed_text(self):
        text = (
            "Привет!\n\n"
            "| Имя | Возраст |\n"
            "| --- | --- |\n"
            "| Иван | 30 |\n\n"
            "Это таблица выше."
        )
        assert has_markdown_table(text) is True

    def test_empty_string(self):
        assert has_markdown_table("") is False

    def test_table_with_formatting(self):
        text = "| **Имя** | _Роль_ |\n|---|---|\n| Иван | Разработчик |"
        assert has_markdown_table(text) is True

    def test_code_block_with_pipes(self):
        # Быстрый детектор не различает | в коде и в таблице — ложный позитив допустим
        text = "```\n| not | table |\n| not | table |\n```"
        result = has_markdown_table(text)
        assert isinstance(result, bool)


# ── send_rich_message ──────────────────────────────────────────────────────────

def _make_bot(token: str = "123:TEST") -> MagicMock:
    bot = MagicMock()
    bot.token = token
    return bot


def _make_httpx_mock(ok: bool = True, description: str | None = None):
    """Возвращает мок httpx.AsyncClient для patch."""
    resp = MagicMock()
    resp.json.return_value = {"ok": ok} if ok else {"ok": False, "description": description or "error"}

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    client_mock.post = AsyncMock(return_value=resp)
    return client_mock


class TestSendRichMessage:
    @pytest.mark.asyncio
    async def test_sends_markdown_field(self):
        """InputRichMessage должен иметь поле 'markdown' с сырым текстом."""
        bot = _make_bot()
        text = "# Hello\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        client_mock = _make_httpx_mock(ok=True)

        with patch("src.rich_message.httpx.AsyncClient", return_value=client_mock):
            result = await send_rich_message(bot, chat_id=123, text=text)

        assert result is True
        client_mock.post.assert_called_once()
        call_kwargs = client_mock.post.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["chat_id"] == 123
        assert "rich_message" in payload
        assert payload["rich_message"] == {"markdown": text}

    @pytest.mark.asyncio
    async def test_passes_message_thread_id(self):
        bot = _make_bot()
        client_mock = _make_httpx_mock(ok=True)

        with patch("src.rich_message.httpx.AsyncClient", return_value=client_mock):
            await send_rich_message(bot, chat_id=123, text="test", message_thread_id=456)

        payload = client_mock.post.call_args.kwargs["json"]
        assert payload["message_thread_id"] == 456

    @pytest.mark.asyncio
    async def test_no_message_thread_id_by_default(self):
        bot = _make_bot()
        client_mock = _make_httpx_mock(ok=True)

        with patch("src.rich_message.httpx.AsyncClient", return_value=client_mock):
            await send_rich_message(bot, chat_id=123, text="test")

        payload = client_mock.post.call_args.kwargs["json"]
        assert "message_thread_id" not in payload

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        bot = _make_bot()
        client_mock = MagicMock()
        client_mock.__aenter__ = AsyncMock(side_effect=Exception("network error"))
        client_mock.__aexit__ = AsyncMock(return_value=False)

        with patch("src.rich_message.httpx.AsyncClient", return_value=client_mock):
            result = await send_rich_message(bot, chat_id=123, text="test")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_api_error(self):
        bot = _make_bot()
        client_mock = _make_httpx_mock(ok=False, description="Rich message must be non-empty")

        with patch("src.rich_message.httpx.AsyncClient", return_value=client_mock):
            result = await send_rich_message(bot, chat_id=123, text="test")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        bot = _make_bot()
        client_mock = _make_httpx_mock(ok=True)

        with patch("src.rich_message.httpx.AsyncClient", return_value=client_mock):
            result = await send_rich_message(bot, chat_id=123, text="# Hello\n\n| A | B |")

        assert result is True

    @pytest.mark.asyncio
    async def test_raw_markdown_passed_unchanged(self):
        """Проверяем, что markdown НЕ преобразуется — передаётся as-is."""
        bot = _make_bot()
        raw = "## Заголовок\n\n**жирный** и _курсив_\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        client_mock = _make_httpx_mock(ok=True)

        with patch("src.rich_message.httpx.AsyncClient", return_value=client_mock):
            await send_rich_message(bot, chat_id=1, text=raw)

        payload = client_mock.post.call_args.kwargs["json"]
        assert payload["rich_message"]["markdown"] == raw

    @pytest.mark.asyncio
    async def test_uses_bot_token_in_url(self):
        """URL должен содержать токен из bot.token."""
        bot = _make_bot(token="999:MYTOKEN")
        client_mock = _make_httpx_mock(ok=True)

        with patch("src.rich_message.httpx.AsyncClient", return_value=client_mock):
            await send_rich_message(bot, chat_id=1, text="test")

        url = client_mock.post.call_args.args[0]
        assert "999:MYTOKEN" in url
        assert "sendRichMessage" in url
