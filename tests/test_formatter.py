"""Тесты для formatter.py — Markdown → Telegram HTML."""

from telegram.constants import ParseMode

from src.formatter import format_for_telegram, markdown_to_html


class TestMarkdownToHtml:
    def test_plain_text_unchanged(self):
        assert markdown_to_html("Обычный текст") == "Обычный текст"

    def test_bold_asterisk(self):
        assert markdown_to_html("**жирный**") == "<b>жирный</b>"

    def test_bold_underscore(self):
        assert markdown_to_html("__жирный__") == "<b>жирный</b>"

    def test_italic(self):
        assert markdown_to_html("_курсив_") == "<i>курсив</i>"

    def test_italic_ignored_inside_word(self):
        assert markdown_to_html("snake_case_var") == "snake_case_var"

    def test_strikethrough(self):
        assert markdown_to_html("~~зачёркнутый~~") == "<s>зачёркнутый</s>"

    def test_header(self):
        assert markdown_to_html("# Заголовок") == "<b>Заголовок</b>"

    def test_link(self):
        result = markdown_to_html("[GitHub](https://github.com)")
        assert result == '<a href="https://github.com">GitHub</a>'

    def test_inline_code(self):
        assert markdown_to_html("`x = 1`") == "<code>x = 1</code>"

    def test_code_block_with_language(self):
        text = "```python\ndef f():\n    pass\n```"
        result = markdown_to_html(text)
        assert '<pre><code class="language-python">' in result
        assert "def f():\n    pass" in result
        assert "</code></pre>" in result

    def test_code_block_no_language(self):
        text = "```\nraw code\n```"
        result = markdown_to_html(text)
        assert "<pre><code>raw code</code></pre>" == result

    def test_html_escape_in_code(self):
        text = '```\nprint("hi & bye")\n```'
        result = markdown_to_html(text)
        assert "&quot;" in result
        assert "&amp;" in result

    def test_html_escape_in_plain(self):
        assert markdown_to_html("a < b & c") == "a &lt; b &amp; c"

    def test_no_formatting_inside_code_block(self):
        # **bold** внутри ``` не должен превращаться в <b>
        text = "```\n**not bold**\n```"
        result = markdown_to_html(text)
        assert "<b>" not in result
        assert "**not bold**" in result

    def test_no_formatting_inside_inline_code(self):
        text = "`**not bold**`"
        result = markdown_to_html(text)
        assert "<b>" not in result


class TestFormatForTelegram:
    def test_plain_text_returns_none_parse_mode(self):
        text, mode = format_for_telegram("Просто текст")
        assert text == "Просто текст"
        assert mode is None

    def test_markdown_returns_html_mode(self):
        text, mode = format_for_telegram("**жирный**")
        assert mode == ParseMode.HTML
        assert text == "<b>жирный</b>"

    def test_code_block_triggers_html(self):
        src = "```python\nprint(1)\n```"
        text, mode = format_for_telegram(src)
        assert mode == ParseMode.HTML
        # Главное для Copy Code: language класс на <code>
        assert '<pre><code class="language-python">' in text

    def test_mixed_content(self):
        src = "**bold** and `code` and\n```js\nx\n```"
        text, mode = format_for_telegram(src)
        assert mode == ParseMode.HTML
        assert "<b>bold</b>" in text
        assert "<code>code</code>" in text
        assert '<pre><code class="language-js">' in text

    def test_backticks_in_plain_text_detected(self):
        # Один backtick — не код; но `x` — уже код
        text, mode = format_for_telegram("используй `git status`")
        assert mode == ParseMode.HTML
        assert "<code>git status</code>" in text

    def test_empty_string(self):
        text, mode = format_for_telegram("")
        assert text == ""
        assert mode is None
