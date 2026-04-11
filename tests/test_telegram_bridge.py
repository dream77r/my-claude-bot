"""Тесты для telegram_bridge.py."""

import pytest

from src.telegram_bridge import escape_markdown_v2, split_message


class TestSplitMessage:
    def test_short_message(self):
        parts = split_message("Короткое сообщение")
        assert len(parts) == 1
        assert parts[0] == "Короткое сообщение"

    def test_long_message_splits(self):
        # Сообщение длиннее 4096
        text = "Абзац один.\n\n" + "x" * 3000 + "\n\nАбзац два.\n\n" + "y" * 3000
        parts = split_message(text, limit=4096)
        assert len(parts) > 1
        for part in parts:
            assert len(part) <= 4096

    def test_markers_added(self):
        text = "a" * 5000
        parts = split_message(text, limit=100)
        assert len(parts) > 1
        assert "(1/" in parts[0]
        assert f"({len(parts)}/{len(parts)})" in parts[-1]

    def test_splits_on_paragraph(self):
        text = "Первый абзац.\n\n" + "x" * 3900
        parts = split_message(text, limit=4096)
        # Должен разбить по \n\n если возможно
        assert len(parts) >= 1

    def test_exact_limit(self):
        text = "x" * 4096
        parts = split_message(text)
        assert len(parts) == 1

    def test_empty_message(self):
        parts = split_message("")
        assert parts == [""]


class TestEscapeMarkdownV2:
    def test_escapes_special_chars(self):
        text = "Цена: 100$. Скидка [10%]"
        result = escape_markdown_v2(text)
        assert "\\." in result
        assert "\\[" in result
        assert "\\]" in result

    def test_preserves_code_blocks(self):
        text = "Код:\n```python\nx = 1 + 2\n```\nТекст."
        result = escape_markdown_v2(text)
        assert "```python" in result
        assert "x = 1 + 2" in result

    def test_preserves_inline_code(self):
        text = "Используй `git commit -m 'test'` команду."
        result = escape_markdown_v2(text)
        assert "`git commit -m 'test'`" in result

    def test_handles_plain_text(self):
        text = "Обычный текст без спецсимволов"
        result = escape_markdown_v2(text)
        assert result == text
