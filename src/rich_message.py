"""
Rich Message support — Telegram Bot API 10.1.

Конвертирует Markdown → InputRichMessage для метода sendRichMessage.
Поддерживает таблицы, заголовки, параграфы, списки и блоки кода.

Используется только если текст содержит markdown-таблицы.
Fallback: если sendRichMessage упал → HTML (ParseMode.HTML).

Документация API:
  https://core.telegram.org/bots/api#sendrichmessage
  https://core.telegram.org/bots/api#inputrichmessage
  https://core.telegram.org/bots/api#richblock
  https://core.telegram.org/bots/api#richtext
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Скомпилированные regex ──────────────────────────────────────────────────

# Быстрый тест: есть ли строка вида | ... | ... |
_TABLE_ROW_RE = re.compile(r"^\|.+\|.*$", re.MULTILINE)

# Строка-разделитель таблицы: | :---: | --- | :--- |
_TABLE_SEP_RE = re.compile(r"^\|[\s\-:]+(?:\|[\s\-:]+)*\|?\s*$")

# Блок кода (с языком или без)
_CODE_BLOCK_RE = re.compile(r"(```(?:[^\n`]*)?\n[\s\S]*?```|```[\s\S]*?```)")
_CODE_BLOCK_WITH_LANG_RE = re.compile(r"```([^\n`]*)\n([\s\S]*?)```")
_BARE_FENCE_RE = re.compile(r"^```|```$")

# Inline-форматирование
_BOLD_RE = re.compile(r"\*\*([\s\S]+?)\*\*|__([\s\S]+?)__")
_ITALIC_RE = re.compile(r"(?<![a-zA-Z0-9])_((?:[^_\n])+)_(?![a-zA-Z0-9])")
_CODE_INLINE_RE = re.compile(r"`([^`\n]+)`")

# Структурные элементы
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_UNORDERED_LIST_RE = re.compile(r"^[-*+]\s+")
_ORDERED_LIST_RE = re.compile(r"^\d+\.\s+")


# ── Public API ──────────────────────────────────────────────────────────────

def has_markdown_table(text: str) -> bool:
    """Проверить, содержит ли текст markdown-таблицу.

    Таблица = хотя бы 2 строки вида | ... | ... |
    (заголовок + разделитель, или заголовок + данные).
    """
    rows = _TABLE_ROW_RE.findall(text)
    return len(rows) >= 2


def markdown_to_rich_message(text: str) -> dict[str, Any]:
    """Конвертировать Markdown → InputRichMessage dict для do_api_request.

    Поддерживает:
    - Параграфы с **bold** / _italic_ / `code` форматированием
    - Заголовки (# ## ###) → RichBlockSectionHeading
    - Таблицы (| col | col |) → RichBlockTable
    - Списки (- item / 1. item) → RichBlockList
    - Блоки кода (```...```) → RichBlockPreformatted

    Returns:
        Словарь InputRichMessage с ключом "blocks".
    """
    blocks: list[dict] = []

    # Разбить на сегменты: чётные — обычный текст, нечётные — блоки кода
    segments = _CODE_BLOCK_RE.split(text)

    for idx, segment in enumerate(segments):
        if idx % 2 == 1:
            # Блок кода
            m = _CODE_BLOCK_WITH_LANG_RE.match(segment)
            if m:
                lang = m.group(1).strip()
                code = m.group(2).rstrip("\n")
                blocks.append(_preformatted_block(code, lang))
            else:
                inner = _BARE_FENCE_RE.sub("", segment).strip()
                blocks.append(_preformatted_block(inner))
            continue

        # Текстовый сегмент: разобрать построчно
        _parse_text_segment(segment, blocks)

    return {"blocks": blocks}


async def send_rich_message(
    bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
) -> bool:
    """Отправить сообщение через sendRichMessage API (Bot API 10.1).

    Returns:
        True  — отправлено успешно.
        False — ошибка, нужен fallback на HTML.
    """
    try:
        rich_msg = markdown_to_rich_message(text)

        if not rich_msg.get("blocks"):
            logger.debug("send_rich_message: нет блоков, пропускаем")
            return False

        api_kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": rich_msg,
        }
        if message_thread_id is not None:
            api_kwargs["message_thread_id"] = message_thread_id

        await bot.do_api_request(
            endpoint="sendRichMessage",
            api_kwargs=api_kwargs,
        )
        logger.info(
            f"sendRichMessage OK: chat={chat_id}, blocks={len(rich_msg['blocks'])}"
        )
        return True

    except Exception as e:
        logger.warning(f"sendRichMessage failed → fallback на HTML: {e}")
        return False


# ── RichText builders ────────────────────────────────────────────────────────

def _plain(text: str) -> dict:
    """RichTextPlain — обычный текст без форматирования."""
    return {"type": "plain", "text": text}


def _bold(inner: dict) -> dict:
    """RichTextBold — жирный текст."""
    return {"type": "bold", "text": inner}


def _italic(inner: dict) -> dict:
    """RichTextItalic — курсив."""
    return {"type": "italic", "text": inner}


def _code_inline(inner: dict) -> dict:
    """RichTextCode — inline-код."""
    return {"type": "code", "text": inner}


def _texts(parts: list[dict]) -> dict:
    """RichTexts — объединить несколько фрагментов в один элемент."""
    if len(parts) == 1:
        return parts[0]
    return {"type": "texts", "texts": parts}


def _parse_inline(text: str) -> dict:
    """Парсить inline-форматирование строки → RichText.

    Обрабатывает: **bold**, _italic_, `code`.
    Возвращает RichText dict.
    """
    if not text:
        return _plain("")

    parts: list[dict] = []
    pos = 0

    # Паттерны с приоритетом поиска
    patterns: list[tuple[str, re.Pattern]] = [
        ("bold", _BOLD_RE),
        ("italic", _ITALIC_RE),
        ("code", _CODE_INLINE_RE),
    ]

    while pos < len(text):
        # Найти ближайшее совпадение
        earliest_match: re.Match | None = None
        earliest_type: str = ""
        earliest_start = len(text)

        for ptype, pattern in patterns:
            m = pattern.search(text, pos)
            if m and m.start() < earliest_start:
                earliest_match = m
                earliest_type = ptype
                earliest_start = m.start()

        if earliest_match is None:
            # Остаток — plain text
            tail = text[pos:]
            if tail:
                parts.append(_plain(tail))
            break

        # Plain text перед совпадением
        if earliest_start > pos:
            parts.append(_plain(text[pos:earliest_start]))

        m = earliest_match
        if earliest_type == "bold":
            inner_text = m.group(1) if m.group(1) is not None else m.group(2)
            parts.append(_bold(_parse_inline(inner_text)))
        elif earliest_type == "italic":
            parts.append(_italic(_parse_inline(m.group(1))))
        elif earliest_type == "code":
            parts.append(_code_inline(_plain(m.group(1))))

        pos = m.end()

    if not parts:
        return _plain("")
    return _texts(parts)


# ── RichBlock builders ────────────────────────────────────────────────────────

def _paragraph_block(text: str) -> dict:
    """RichBlockParagraph."""
    return {"type": "paragraph", "text": _parse_inline(text)}


def _heading_block(text: str) -> dict:
    """RichBlockSectionHeading."""
    return {"type": "section_heading", "text": _parse_inline(text)}


def _preformatted_block(code: str, language: str = "") -> dict:
    """RichBlockPreformatted — блок кода."""
    block: dict = {"type": "preformatted", "text": _plain(code)}
    if language:
        block["language"] = language
    return block


def _list_block(items: list[str], ordered: bool = False) -> dict:
    """RichBlockList — нумерованный или маркированный список."""
    return {
        "type": "list",
        "items": [{"label": _parse_inline(item)} for item in items],
        "is_ordered": ordered,
    }


def _table_block(
    data_rows: list[list[str]],
    headers: list[str] | None = None,
) -> dict:
    """RichBlockTable из списка строк.

    Args:
        data_rows: строки данных (без заголовка).
        headers: список заголовков (если есть).
    """
    rich_rows: list[dict] = []

    # Строка заголовка
    if headers:
        header_cells = [
            {
                "blocks": [_paragraph_block(cell)],
                "header": True,
            }
            for cell in headers
        ]
        rich_rows.append({"cells": header_cells})

    # Строки данных
    for row in data_rows:
        cells = [{"blocks": [_paragraph_block(cell)]} for cell in row]
        rich_rows.append({"cells": cells})

    return {"type": "table", "rows": rich_rows}


# ── Текстовый парсер ──────────────────────────────────────────────────────────

def _parse_markdown_table(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    """Парсить строки markdown-таблицы.

    Returns:
        (headers, data_rows)
    """
    headers: list[str] = []
    data_rows: list[list[str]] = []
    first_data = True

    for line in lines:
        stripped = line.strip()

        # Строка-разделитель | --- | --- |
        if _TABLE_SEP_RE.match(stripped):
            continue

        # Парсить ячейки: убрать внешние | и разбить по |
        inner = stripped.strip("|")
        cells = [c.strip() for c in inner.split("|")]

        if first_data:
            headers = cells
            first_data = False
        else:
            data_rows.append(cells)

    return headers, data_rows


def _parse_text_segment(text: str, blocks: list[dict]) -> None:
    """Парсить текстовый сегмент (без блоков кода) → добавить блоки в список."""
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Пустая строка
        if not stripped:
            i += 1
            continue

        # Заголовок # / ## / ###
        m = _HEADER_RE.match(stripped)
        if m:
            blocks.append(_heading_block(m.group(2)))
            i += 1
            continue

        # Таблица: строки начинаются с |
        if stripped.startswith("|"):
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            if table_lines:
                hdrs, rows = _parse_markdown_table(table_lines)
                blocks.append(_table_block(rows, hdrs))
            continue

        # Маркированный список
        if _UNORDERED_LIST_RE.match(stripped):
            items: list[str] = []
            while i < len(lines) and _UNORDERED_LIST_RE.match(lines[i].strip()):
                item_text = _UNORDERED_LIST_RE.sub("", lines[i].strip(), count=1)
                items.append(item_text)
                i += 1
            if items:
                blocks.append(_list_block(items, ordered=False))
            continue

        # Нумерованный список
        if _ORDERED_LIST_RE.match(stripped):
            items = []
            while i < len(lines) and _ORDERED_LIST_RE.match(lines[i].strip()):
                item_text = _ORDERED_LIST_RE.sub("", lines[i].strip(), count=1)
                items.append(item_text)
                i += 1
            if items:
                blocks.append(_list_block(items, ordered=True))
            continue

        # Параграф: объединяем смежные строки до пустой строки или структурного элемента
        para_lines: list[str] = []
        while i < len(lines):
            l = lines[i].strip()
            if not l:
                break
            if (
                l.startswith("|")
                or _HEADER_RE.match(l)
                or _UNORDERED_LIST_RE.match(l)
                or _ORDERED_LIST_RE.match(l)
            ):
                break
            para_lines.append(l)
            i += 1

        if para_lines:
            para_text = " ".join(para_lines)
            blocks.append(_paragraph_block(para_text))
