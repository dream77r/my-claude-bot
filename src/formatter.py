"""
Форматирование Markdown → Telegram HTML.

Все regex, которые используются на каждый вызов, скомпилированы на уровне
модуля (а не внутри функций) — это экономит CPU для горячего пути
отправки сообщений в чат.
"""

import html as _html
import logging
import re

from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

# Telegram лимит на длину сообщения
TG_MESSAGE_LIMIT = 4096

# ---- Скомпилированные regex (горячий путь) ----

# Блоки кода в escape_markdown_v2
_FENCE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_INLINE_BACKTICK_RE = re.compile(r"`[^`]+`")

# Спецсимволы MarkdownV2 (кроме *, _, ~, ||)
_MD_V2_ESCAPE_RE = re.compile(r"[\\\[\]()>#+\-=|{}.!]")

# Блоки кода (с языком или без) — общий для markdown_to_html и split_message
_CODE_BLOCK_RE = re.compile(r"(```(?:[^\n`]*)?\n[\s\S]*?```|```[\s\S]*?```)")

# Блок кода с языком и новой строкой
_CODE_BLOCK_WITH_LANG_RE = re.compile(r"```([^\n`]*)\n([\s\S]*?)```")

# Обрезка голых ```
_BARE_FENCE_RE = re.compile(r"^```|```$")

# Inline-код (`...`)
_INLINE_CODE_RE = re.compile(r"(`[^`\n]+`)")

# Заголовки Markdown (# Title)
_HEADER_RE = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)

# Жирный **text** и __text__
_BOLD_ASTERISK_RE = re.compile(r"\*\*([\s\S]+?)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"__([\s\S]+?)__")

# Зачёркнутый ~~text~~
_STRIKE_RE = re.compile(r"~~([\s\S]+?)~~")

# Курсив _text_ (не внутри слова)
_ITALIC_RE = re.compile(r"(?<![a-zA-Z0-9])_([^_\n]+)_(?![a-zA-Z0-9])")

# Ссылки [text](url)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Быстрый тест: есть ли в тексте Markdown вообще
_HAS_MARKDOWN_RE = re.compile(
    r"```|`[^`]|\*\*|__|\[.+\]\(.+\)|^#{1,6} |~~",
    re.MULTILINE,
)


def escape_markdown_v2(text: str) -> str:
    """Экранировать спецсимволы для MarkdownV2, сохраняя форматирование."""
    # Сначала обработаем блоки кода — их не трогаем
    code_blocks = []
    inline_codes = []

    # Извлечь ``` блоки
    def save_code_block(match):
        code_blocks.append(match.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = _FENCE_BLOCK_RE.sub(save_code_block, text)

    # Извлечь `inline` код
    def save_inline_code(match):
        inline_codes.append(match.group(0))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = _INLINE_BACKTICK_RE.sub(save_inline_code, text)

    # Экранировать спецсимволы MarkdownV2 (кроме *, _, ~, ||)
    # Эти символы нужно экранировать: _ * [ ] ( ) ~ ` > # + - = | { } . !
    # Но *, _, ~ используются для форматирования — экранируем только если не парные
    text = _MD_V2_ESCAPE_RE.sub(r"\\\g<0>", text)

    # Вернуть блоки кода
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", code)

    return text


def markdown_to_html(text: str) -> str:
    """Конвертировать Claude Markdown в Telegram HTML-форматирование.

    Поддерживает:
    - Блоки кода с подсветкой синтаксиса (```lang\\ncode```) → <pre><code>
    - Inline-код (`code`) → <code>
    - Жирный (**text** и __text__) → <b>
    - Курсив (_text_) → <i>
    - Зачёркнутый (~~text~~) → <s>
    - Заголовки (# text) → <b>
    - Ссылки ([text](url)) → <a href>
    """
    result = []

    # Шаг 1: разбить на блоки кода и обычный текст
    # Паттерн: ```lang\n...\n``` или просто ```\n...\n```
    parts = _CODE_BLOCK_RE.split(text)

    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Блок кода — определить язык
            m = _CODE_BLOCK_WITH_LANG_RE.match(part)
            if m:
                lang = m.group(1).strip()
                code = _html.escape(m.group(2).rstrip("\n"))
                if lang:
                    result.append(f'<pre><code class="language-{lang}">{code}</code></pre>')
                else:
                    result.append(f"<pre><code>{code}</code></pre>")
            else:
                # Блок без языка и без новой строки
                inner = _BARE_FENCE_RE.sub("", part).strip()
                result.append(f"<pre><code>{_html.escape(inner)}</code></pre>")
            continue

        # Шаг 2: разбить на inline-код и обычный текст
        subparts = _INLINE_CODE_RE.split(part)
        for j, subpart in enumerate(subparts):
            if j % 2 == 1:
                # Inline-код
                code = _html.escape(subpart[1:-1])
                result.append(f"<code>{code}</code>")
                continue

            # Шаг 3: обычный текст — экранировать HTML, затем применить форматирование
            s = _html.escape(subpart)

            # Заголовки (# text) — до остального, чтобы не ломать ссылки
            s = _HEADER_RE.sub(r"<b>\1</b>", s)

            # Жирный **text** и __text__
            s = _BOLD_ASTERISK_RE.sub(r"<b>\1</b>", s)
            s = _BOLD_UNDERSCORE_RE.sub(r"<b>\1</b>", s)

            # Зачёркнутый ~~text~~
            s = _STRIKE_RE.sub(r"<s>\1</s>", s)

            # Курсив _text_ (не внутри слова: no_match_here)
            s = _ITALIC_RE.sub(r"<i>\1</i>", s)

            # Ссылки [text](url) — html.escape уже экранировал & в url как &amp;, это ок
            s = _LINK_RE.sub(r'<a href="\2">\1</a>', s)

            result.append(s)

    return "".join(result)


def format_for_telegram(text: str) -> tuple[str, str | None]:
    """Конвертировать ответ агента в HTML для Telegram.

    Returns:
        (formatted_text, parse_mode) — parse_mode=None если конвертация не нужна
    """
    # Если в тексте нет markdown-разметки — отправить как plain text
    has_markdown = bool(_HAS_MARKDOWN_RE.search(text))
    if not has_markdown:
        return text, None
    try:
        html = markdown_to_html(text)
        return html, ParseMode.HTML
    except Exception as e:
        logger.warning(f"format_for_telegram: markdown→HTML failed: {e}")
        return text, None


def split_message(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    """
    Разбить длинное сообщение на части с маркером "(n/m)".
    Никогда не разбивает внутри ``` блоков кода.
    Разбивает обычный текст по параграфам, строкам, потом жёстко.
    """
    if len(text) <= limit:
        return [text]

    # Место для маркера "(nn/mm)\n"
    effective_limit = limit - 10

    # Нечётные сегменты — блоки кода (нельзя разбивать)
    segments = _CODE_BLOCK_RE.split(text)

    def _flush_text(buf: str) -> list[str]:
        """Разбить обычный текст на куски ≤ effective_limit."""
        result = []
        remaining = buf
        while len(remaining) > effective_limit:
            cut = remaining.rfind("\n\n", 0, effective_limit)
            if cut > effective_limit // 3:
                result.append(remaining[:cut].rstrip())
                remaining = remaining[cut + 2:]
                continue
            cut = remaining.rfind("\n", 0, effective_limit)
            if cut > effective_limit // 3:
                result.append(remaining[:cut].rstrip())
                remaining = remaining[cut + 1:]
                continue
            cut = remaining.rfind(" ", 0, effective_limit)
            if cut > effective_limit // 3:
                result.append(remaining[:cut].rstrip())
                remaining = remaining[cut + 1:]
                continue
            result.append(remaining[:effective_limit])
            remaining = remaining[effective_limit:]
        if remaining.strip():
            result.append(remaining)
        return result

    parts: list[str] = []
    current = ""

    for i, segment in enumerate(segments):
        is_code = i % 2 == 1

        if is_code:
            if len(current) + len(segment) <= effective_limit:
                current += segment
            else:
                # Сброс текстового буфера
                flushed = _flush_text(current)
                if len(flushed) > 1:
                    parts.extend(flushed[:-1])
                    current = flushed[-1]
                elif flushed:
                    current = flushed[0]
                # Добавить блок кода
                if len(current) + len(segment) <= effective_limit:
                    current += segment
                else:
                    if current.strip():
                        parts.append(current.rstrip())
                    # Блок кода превышает лимит — отправить целиком
                    if len(segment) > effective_limit:
                        parts.append(segment)
                        current = ""
                    else:
                        current = segment
        else:
            current += segment

    if current.strip():
        flushed = _flush_text(current)
        parts.extend(flushed)

    parts = [p for p in parts if p.strip()]
    if not parts:
        return [text]

    if len(parts) > 1:
        total = len(parts)
        parts = [f"({i + 1}/{total})\n{part}" for i, part in enumerate(parts)]

    return parts
