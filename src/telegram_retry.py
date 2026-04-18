"""
Retry-обёртка для вызовов Telegram Bot API.

Telegram периодически отвечает 502/503, рвёт соединение, или требует
`retry_after` на flood control. Вместо того чтобы проглотить ошибку и
потерять сообщение, повторяем transient-ошибки с exponential backoff.

Использование:

    from .telegram_retry import tg_retry

    await tg_retry(lambda: context.bot.send_message(
        chat_id=chat_id, text="hi"
    ))

Что retrying:
- NetworkError / TimedOut — сеть моргнула, повторяем.
- RetryAfter — Telegram flood control, спим retry_after и повторяем
  (не считается попыткой, т.к. задержка задана сервером).

Что НЕ retrying:
- BadRequest / Forbidden / ChatMigrated — клиентские ошибки, повтор не
  исправит. Прокидываем наверх.
- Прочие TelegramError — прокидываем.
- Любые не-телеграмные исключения — прокидываем.
"""

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_ATTEMPTS = 4  # 1 основной + 3 retry
DEFAULT_BASE_DELAY = 1.0  # 1s, 2s, 4s между попытками
MAX_RETRY_AFTER = 60.0  # страховка от абсурдных значений


async def tg_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    op: str = "telegram_api",
) -> T:
    """
    Вызвать Telegram API с retry на transient ошибки.

    Args:
        factory: фабрика корутины — `lambda: bot.send_message(...)`.
            Пересоздаётся на каждую попытку (корутину нельзя await дважды).
        attempts: максимум попыток суммарно (включая первую).
        base_delay: базовая задержка, растёт экспоненциально.
        op: метка для логов.

    Returns:
        Результат корутины.

    Raises:
        Последнюю перехваченную ошибку, если все попытки исчерпаны.
        Любую не-transient ошибку сразу.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await factory()
        except RetryAfter as e:
            last_error = e
            if attempt == attempts - 1:
                logger.warning(f"{op}: exhausted {attempts} attempts (flood): {e}")
                raise
            delay = min(float(e.retry_after) + 0.5, MAX_RETRY_AFTER)
            logger.info(f"{op}: RetryAfter {e.retry_after}s, sleeping")
            await asyncio.sleep(delay)
        except BadRequest:
            # В PTB BadRequest наследует NetworkError (исторически), но это
            # client-error (неверный chat_id, текст > лимита, parse error и
            # т.п.) — retry не поможет, прокидываем.
            raise
        except (NetworkError, TimedOut) as e:
            last_error = e
            if attempt == attempts - 1:
                logger.warning(f"{op}: exhausted {attempts} attempts: {e}")
                raise
            delay = base_delay * (2**attempt)
            logger.info(
                f"{op}: transient error ({type(e).__name__}: {e}), "
                f"retry {attempt + 1}/{attempts - 1} after {delay}s"
            )
            await asyncio.sleep(delay)
    # Unreachable — цикл либо return, либо raise.
    assert last_error is not None
    raise last_error
