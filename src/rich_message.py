"""
Rich Message support — Telegram Bot API 10.1.

Отправляет Markdown через sendRichMessage (нативный рендеринг таблиц, заголовков и т.д.).

InputRichMessage формат:
  {"markdown": "<raw markdown text>"}   — GitHub-flavored Markdown

Fallback: если sendRichMessage упал → HTML (ParseMode.HTML).

Документация API:
  https://core.telegram.org/bots/api#sendrichmessage
  https://core.telegram.org/bots/api#inputrichmessage

Примечание: PTB (do_api_request) не умеет корректно сериализовать вложенные dict
в JSON — отправляет как form-data со str(dict). Поэтому используем httpx напрямую.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Быстрые тесты наличия таблиц/markdown ───────────────────────────────────

# Таблица = хотя бы 2 строки вида | ... | ... |
_TABLE_ROW_RE = re.compile(r"^\|.+\|.*$", re.MULTILINE)


def has_markdown_table(text: str) -> bool:
    """Проверить, содержит ли текст markdown-таблицу.

    Таблица = хотя бы 2 строки вида | ... | ... |
    (заголовок + разделитель, или заголовок + данные).
    """
    rows = _TABLE_ROW_RE.findall(text)
    return len(rows) >= 2


# ── Public API ──────────────────────────────────────────────────────────────

async def send_rich_message(
    bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
) -> bool:
    """Отправить сообщение через sendRichMessage API (Bot API 10.1).

    InputRichMessage = {"markdown": "<raw markdown>"} — сервер сам рендерит
    таблицы, заголовки, списки и т.д. нативно.

    Использует httpx напрямую (PTB некорректно сериализует вложенные dict в form-data).

    Returns:
        True  — отправлено успешно.
        False — ошибка, нужен fallback на HTML.
    """
    try:
        token = bot.token
        url = f"https://api.telegram.org/bot{token}/sendRichMessage"

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": text},
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

        if data.get("ok"):
            logger.info(f"sendRichMessage OK: chat={chat_id}, len={len(text)}")
            return True
        else:
            logger.warning(f"sendRichMessage API error → fallback на HTML: {data.get('description')}")
            return False

    except Exception as e:
        logger.warning(f"sendRichMessage failed → fallback на HTML: {e}")
        return False
