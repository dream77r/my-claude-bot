"""
Статусное сообщение с защитой от rate limit (delta coalescing).
"""

import asyncio
import logging
import time

from telegram.constants import ChatAction

from .formatter import TG_MESSAGE_LIMIT, format_for_telegram

logger = logging.getLogger(__name__)

# Минимальный интервал между edit-сообщениями (coalescing)
EDIT_MIN_INTERVAL = 0.5

# Интервал для streaming текста (быстрее чем tool hints)
STREAM_EDIT_INTERVAL = 0.3

# Интервал typing indicator (секунды)
TYPING_KEEPALIVE_INTERVAL = 4


class StatusMessage:
    """
    Сообщение-статус с защитой от rate limit (delta coalescing).

    Показывает пользователю что делает агент (tool hints и streaming текст),
    ограничивая edit-запросы до 1 раз в EDIT_MIN_INTERVAL секунд.

    Поддерживает typing keepalive и финальный edit вместо delete+new.
    """

    def __init__(self, chat_id: int, context, thread_id: int | None = None):
        self.chat_id = chat_id
        self.context = context
        self.thread_id = thread_id
        self.message_id: int | None = None
        self._last_edit: float = 0
        self._pending_text: str | None = None
        self._pending_task: asyncio.Task | None = None
        self._typing_task: asyncio.Task | None = None
        self._thinking_start: float | None = None
        self._thinking_timer_task: asyncio.Task | None = None
        self._is_streaming: bool = False
        self._last_text: str = ""

    def current_text(self) -> str:
        """Последний текст, показанный пользователю (для stream interruption)."""
        return self._last_text

    async def show(self, text: str, streaming: bool = False) -> None:
        """Показать или обновить статус (с coalescing)."""
        self._is_streaming = streaming
        self._last_text = text
        interval = STREAM_EDIT_INTERVAL if streaming else EDIT_MIN_INTERVAL
        now = time.monotonic()
        elapsed = now - self._last_edit

        if elapsed >= interval:
            await self._do_edit(text)
        else:
            # Отложить обновление
            self._pending_text = text
            if not self._pending_task or self._pending_task.done():
                delay = interval - elapsed
                self._pending_task = asyncio.create_task(
                    self._delayed_edit(delay)
                )

    async def finalize(self, text: str) -> bool:
        """
        Финальное обновление — edit вместо delete+new.

        Returns:
            True если удалось отредактировать на месте.
            False если нужен новый send (длинный текст, ошибка edit).
        """
        self._stop_typing()
        self.stop_thinking_timer()
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()

        if not self.message_id:
            return False

        # Если текст влезает в одно сообщение — edit на месте
        if len(text) <= TG_MESSAGE_LIMIT:
            formatted_text, fmt_parse_mode = format_for_telegram(text)

            # Если ответ содержит блок кода — удалить статус и отправить
            # новым сообщением, иначе Telegram не показывает кнопку Copy Code на edit.
            has_code_block = fmt_parse_mode and "<pre><code" in formatted_text
            if has_code_block:
                await self.cleanup()
                return False

            try:
                await self.context.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=formatted_text,
                    parse_mode=fmt_parse_mode,
                )
                self.message_id = None  # Больше не управляем этим сообщением
                return True
            except Exception as e:
                # "message is not modified" — текст уже тот же (из text_delta)
                if "is not modified" in str(e):
                    self.message_id = None
                    return True
                # Если HTML не парсится — попробовать plain text
                if fmt_parse_mode:
                    try:
                        await self.context.bot.edit_message_text(
                            chat_id=self.chat_id,
                            message_id=self.message_id,
                            text=text,
                        )
                        self.message_id = None
                        return True
                    except Exception:
                        pass
                logger.debug(f"Finalize edit failed: {e}")
                # Если edit не сработал — fallback на delete+send
                await self.cleanup()
                return False

        # Длинный текст — нужен split, delete статус
        await self.cleanup()
        return False

    async def cleanup(self) -> None:
        """Удалить статус-сообщение после завершения."""
        self._stop_typing()
        self.stop_thinking_timer()
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()
        if self.message_id:
            try:
                await self.context.bot.delete_message(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                )
            except Exception:
                pass  # Сообщение могло быть уже удалено
            self.message_id = None

    def start_typing(self) -> None:
        """Запустить typing keepalive (ChatAction.TYPING каждые 4 секунды)."""
        if self._typing_task and not self._typing_task.done():
            return
        self._typing_task = asyncio.create_task(self._typing_loop())

    def _stop_typing(self) -> None:
        """Остановить typing keepalive."""
        if self._typing_task and not self._typing_task.done():
            self._typing_task.cancel()
            self._typing_task = None

    async def _typing_loop(self) -> None:
        """Цикл отправки ChatAction.TYPING."""
        try:
            while True:
                try:
                    await self.context.bot.send_chat_action(
                        chat_id=self.chat_id,
                        action=ChatAction.TYPING,
                        message_thread_id=self.thread_id,
                    )
                except Exception:
                    pass
                await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL)
        except asyncio.CancelledError:
            pass

    def start_thinking_timer(self) -> None:
        """Запустить таймер 'Думаю... (Xс)', обновляется каждые 10 секунд."""
        self._thinking_start = time.monotonic()
        if self._thinking_timer_task and not self._thinking_timer_task.done():
            return
        self._thinking_timer_task = asyncio.create_task(self._thinking_timer_loop())

    def stop_thinking_timer(self) -> None:
        """Остановить таймер 'Думаю...'."""
        if self._thinking_timer_task and not self._thinking_timer_task.done():
            self._thinking_timer_task.cancel()
            self._thinking_timer_task = None

    async def _thinking_timer_loop(self) -> None:
        """Цикл обновления 'Думаю... (10с)', '(20с)' и т.д."""
        try:
            while True:
                await asyncio.sleep(10)
                if self._thinking_start is None:
                    break
                elapsed = int(time.monotonic() - self._thinking_start)
                await self._do_edit(f"💬 Думаю... ({elapsed}с)")
        except asyncio.CancelledError:
            pass

    async def _delayed_edit(self, delay: float) -> None:
        """Отложенное обновление для coalescing."""
        await asyncio.sleep(delay)
        if self._pending_text:
            text = self._pending_text
            self._pending_text = None
            await self._do_edit(text)

    async def _do_edit(self, text: str) -> None:
        """Непосредственное обновление сообщения."""
        self._last_edit = time.monotonic()
        try:
            if self.message_id:
                await self.context.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text[:TG_MESSAGE_LIMIT],
                )
            else:
                msg = await self.context.bot.send_message(
                    chat_id=self.chat_id,
                    text=text[:TG_MESSAGE_LIMIT],
                    message_thread_id=self.thread_id,
                )
                self.message_id = msg.message_id
        except Exception as e:
            # Telegram может вернуть "message is not modified" — это нормально
            logger.debug(f"Status edit: {e}")
