"""Тесты для rich_message.py — Telegram Bot API 10.1 sendRichMessage."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.rich_message import (
    has_markdown_table,
    markdown_to_rich_message,
    send_rich_message,
    _plain,
    _bold,
    _italic,
    _texts,
    _parse_inline,
    _paragraph_block,
    _heading_block,
    _preformatted_block,
    _list_block,
    _table_block,
)


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
        # Только одна строка с |
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

    def test_code_block_with_pipes(self):
        # Символы | в блоке кода — не таблица (но детектор не смотрит внутрь кода)
        text = "```\n| not | table |\n| not | table |\n```"
        # has_markdown_table — быстрый тест, не различает код/текст
        # (ложный позитив допустим — fallback через except в send_rich_message)
        # Проверяем только что он не падает
        result = has_markdown_table(text)
        assert isinstance(result, bool)

    def test_table_with_formatting(self):
        text = "| **Имя** | _Роль_ |\n|---|---|\n| Иван | Разработчик |"
        assert has_markdown_table(text) is True


# ── _parse_inline ───────────────────────────────────────────────────────────────

class TestParseInline:
    def test_plain_text(self):
        result = _parse_inline("Hello world")
        assert result == {"type": "plain", "text": "Hello world"}

    def test_empty(self):
        result = _parse_inline("")
        assert result == {"type": "plain", "text": ""}

    def test_bold_asterisk(self):
        result = _parse_inline("**жирный**")
        assert result == {"type": "bold", "text": {"type": "plain", "text": "жирный"}}

    def test_italic(self):
        result = _parse_inline("_курсив_")
        assert result == {"type": "italic", "text": {"type": "plain", "text": "курсив"}}

    def test_inline_code(self):
        result = _parse_inline("`code`")
        assert result == {"type": "code", "text": {"type": "plain", "text": "code"}}

    def test_mixed_bold_and_plain(self):
        result = _parse_inline("Привет **мир**!")
        assert result["type"] == "texts"
        texts = result["texts"]
        assert len(texts) == 3
        assert texts[0] == {"type": "plain", "text": "Привет "}
        assert texts[1] == {"type": "bold", "text": {"type": "plain", "text": "мир"}}
        assert texts[2] == {"type": "plain", "text": "!"}

    def test_bold_underscore(self):
        result = _parse_inline("__жирный__")
        assert result == {"type": "bold", "text": {"type": "plain", "text": "жирный"}}

    def test_single_rich_text_not_wrapped(self):
        # Один элемент не оборачивается в texts
        result = _parse_inline("**only bold**")
        assert result["type"] == "bold"


# ── RichBlock builders ───────────────────────────────────────────────────────────

class TestRichBlockBuilders:
    def test_paragraph_block(self):
        block = _paragraph_block("Hello **world**")
        assert block["type"] == "paragraph"
        assert "text" in block

    def test_heading_block(self):
        block = _heading_block("Заголовок")
        assert block["type"] == "section_heading"
        assert block["text"] == {"type": "plain", "text": "Заголовок"}

    def test_preformatted_block_with_language(self):
        block = _preformatted_block("print('hi')", "python")
        assert block["type"] == "preformatted"
        assert block["language"] == "python"
        assert block["text"] == {"type": "plain", "text": "print('hi')"}

    def test_preformatted_block_no_language(self):
        block = _preformatted_block("code here")
        assert block["type"] == "preformatted"
        assert "language" not in block

    def test_list_block_unordered(self):
        block = _list_block(["foo", "bar"], ordered=False)
        assert block["type"] == "list"
        assert block["is_ordered"] is False
        assert len(block["items"]) == 2
        assert block["items"][0]["label"] == {"type": "plain", "text": "foo"}

    def test_list_block_ordered(self):
        block = _list_block(["один", "два"], ordered=True)
        assert block["is_ordered"] is True

    def test_table_block_with_headers(self):
        block = _table_block([["1", "2"]], headers=["A", "B"])
        assert block["type"] == "table"
        rows = block["rows"]
        assert len(rows) == 2  # header row + data row
        # Первая строка — заголовок
        assert rows[0]["cells"][0]["header"] is True
        assert rows[0]["cells"][1]["header"] is True
        # Вторая строка — данные (без header)
        assert "header" not in rows[1]["cells"][0]

    def test_table_block_no_headers(self):
        block = _table_block([["A", "B"], ["C", "D"]])
        assert block["type"] == "table"
        assert len(block["rows"]) == 2
        assert "header" not in block["rows"][0]["cells"][0]

    def test_table_cell_has_blocks(self):
        block = _table_block([["текст"]])
        cell = block["rows"][0]["cells"][0]
        assert "blocks" in cell
        assert cell["blocks"][0]["type"] == "paragraph"


# ── markdown_to_rich_message ───────────────────────────────────────────────────

class TestMarkdownToRichMessageTable:
    def test_simple_table(self):
        text = "| Имя | Возраст |\n| --- | --- |\n| Иван | 30 |"
        result = markdown_to_rich_message(text)

        assert "blocks" in result
        blocks = result["blocks"]
        assert len(blocks) == 1

        table = blocks[0]
        assert table["type"] == "table"
        rows = table["rows"]
        # Заголовок + 1 строка данных
        assert len(rows) == 2
        # Заголовок
        assert rows[0]["cells"][0]["header"] is True
        # Содержимое заголовка
        header_text = rows[0]["cells"][0]["blocks"][0]["text"]["text"]
        assert header_text == "Имя"
        # Данные
        data_text = rows[1]["cells"][0]["blocks"][0]["text"]["text"]
        assert data_text == "Иван"

    def test_table_headers_extracted(self):
        text = "| Поле | Тип | Описание |\n|---|---|---|\n| id | int | ID |"
        result = markdown_to_rich_message(text)

        table = result["blocks"][0]
        assert table["type"] == "table"
        # 3 столбца
        assert len(table["rows"][0]["cells"]) == 3
        # Заголовки
        headers = [c["blocks"][0]["text"]["text"] for c in table["rows"][0]["cells"]]
        assert headers == ["Поле", "Тип", "Описание"]

    def test_table_with_formatted_cells(self):
        text = "| **Имя** | _Роль_ |\n|---|---|\n| Иван | Разраб |"
        result = markdown_to_rich_message(text)

        table = result["blocks"][0]
        # Ячейка с **Имя** — должна парситься как bold
        header_cell_text = table["rows"][0]["cells"][0]["blocks"][0]["text"]
        assert header_cell_text["type"] == "bold"

    def test_multirow_table(self):
        text = (
            "| A | B |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
            "| 3 | 4 |\n"
            "| 5 | 6 |"
        )
        result = markdown_to_rich_message(text)
        table = result["blocks"][0]
        # 1 заголовок + 3 строки данных
        assert len(table["rows"]) == 4


class TestMarkdownToRichMessageMixed:
    def test_text_then_table(self):
        text = "Описание:\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        result = markdown_to_rich_message(text)

        blocks = result["blocks"]
        assert len(blocks) == 2
        assert blocks[0]["type"] == "paragraph"
        assert blocks[1]["type"] == "table"

    def test_table_then_text(self):
        text = "| A | B |\n|---|---|\n| 1 | 2 |\n\nПримечание после таблицы."
        result = markdown_to_rich_message(text)

        blocks = result["blocks"]
        assert len(blocks) == 2
        assert blocks[0]["type"] == "table"
        assert blocks[1]["type"] == "paragraph"

    def test_text_table_list(self):
        text = (
            "Введение.\n\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n\n"
            "- пункт 1\n"
            "- пункт 2"
        )
        result = markdown_to_rich_message(text)
        blocks = result["blocks"]
        types = [b["type"] for b in blocks]
        assert "paragraph" in types
        assert "table" in types
        assert "list" in types

    def test_table_with_code_block(self):
        text = (
            "| Метод | Описание |\n"
            "|---|---|\n"
            "| POST | Создать |\n\n"
            "```python\nresponse = requests.post(url)\n```"
        )
        result = markdown_to_rich_message(text)
        blocks = result["blocks"]
        assert blocks[0]["type"] == "table"
        assert blocks[1]["type"] == "preformatted"
        assert blocks[1]["language"] == "python"

    def test_empty_result_has_blocks_key(self):
        result = markdown_to_rich_message("")
        assert "blocks" in result


class TestMarkdownToRichMessageHeadings:
    def test_h1_becomes_section_heading(self):
        result = markdown_to_rich_message("# Главный заголовок")
        blocks = result["blocks"]
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section_heading"
        assert blocks[0]["text"]["text"] == "Главный заголовок"

    def test_h2_heading(self):
        result = markdown_to_rich_message("## Подзаголовок")
        assert result["blocks"][0]["type"] == "section_heading"

    def test_h3_heading(self):
        result = markdown_to_rich_message("### Ещё один")
        assert result["blocks"][0]["type"] == "section_heading"

    def test_multiple_headings(self):
        text = "# Раздел 1\n\nТекст.\n\n## Раздел 2"
        result = markdown_to_rich_message(text)
        types = [b["type"] for b in result["blocks"]]
        assert types.count("section_heading") == 2
        assert "paragraph" in types

    def test_heading_with_bold(self):
        result = markdown_to_rich_message("# **Жирный** заголовок")
        block = result["blocks"][0]
        assert block["type"] == "section_heading"
        # Текст должен содержать bold
        text_elem = block["text"]
        assert text_elem["type"] == "texts"

    def test_heading_text_content(self):
        result = markdown_to_rich_message("# Итоги")
        block = result["blocks"][0]
        assert block["text"]["text"] == "Итоги"


class TestMarkdownToRichMessageLists:
    def test_unordered_list(self):
        text = "- первый\n- второй\n- третий"
        result = markdown_to_rich_message(text)
        block = result["blocks"][0]
        assert block["type"] == "list"
        assert block["is_ordered"] is False
        assert len(block["items"]) == 3

    def test_ordered_list(self):
        text = "1. первый\n2. второй\n3. третий"
        result = markdown_to_rich_message(text)
        block = result["blocks"][0]
        assert block["type"] == "list"
        assert block["is_ordered"] is True
        assert len(block["items"]) == 3

    def test_list_item_content(self):
        text = "- **важный** пункт"
        result = markdown_to_rich_message(text)
        item = result["blocks"][0]["items"][0]
        assert item["label"]["type"] == "texts"  # bold + plain mixed


# ── send_rich_message ───────────────────────────────────────────────────────────

class TestSendRichMessage:
    @pytest.mark.asyncio
    async def test_success(self):
        """sendRichMessage успешно отправлен → возвращает True."""
        bot = MagicMock()
        bot.do_api_request = AsyncMock(return_value=None)

        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = await send_rich_message(bot, chat_id=123, text=text)

        assert result is True
        bot.do_api_request.assert_called_once()

        call_kwargs = bot.do_api_request.call_args
        assert call_kwargs.kwargs["endpoint"] == "sendRichMessage"
        api_kwargs = call_kwargs.kwargs["api_kwargs"]
        assert api_kwargs["chat_id"] == 123
        assert "rich_message" in api_kwargs
        assert "blocks" in api_kwargs["rich_message"]

    @pytest.mark.asyncio
    async def test_with_thread_id(self):
        """message_thread_id передаётся в API."""
        bot = MagicMock()
        bot.do_api_request = AsyncMock(return_value=None)

        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        await send_rich_message(bot, chat_id=123, text=text, message_thread_id=456)

        api_kwargs = bot.do_api_request.call_args.kwargs["api_kwargs"]
        assert api_kwargs["message_thread_id"] == 456

    @pytest.mark.asyncio
    async def test_fallback_on_exception(self):
        """При исключении возвращает False (не бросает)."""
        bot = MagicMock()
        bot.do_api_request = AsyncMock(side_effect=Exception("API Error"))

        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = await send_rich_message(bot, chat_id=123, text=text)

        assert result is False

    @pytest.mark.asyncio
    async def test_empty_text_returns_false(self):
        """Пустой текст (нет блоков) → False."""
        bot = MagicMock()
        bot.do_api_request = AsyncMock(return_value=None)

        result = await send_rich_message(bot, chat_id=123, text="")

        assert result is False
        bot.do_api_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_thread_id_not_in_kwargs(self):
        """message_thread_id=None не добавляется в api_kwargs."""
        bot = MagicMock()
        bot.do_api_request = AsyncMock(return_value=None)

        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        await send_rich_message(bot, chat_id=123, text=text, message_thread_id=None)

        api_kwargs = bot.do_api_request.call_args.kwargs["api_kwargs"]
        assert "message_thread_id" not in api_kwargs
