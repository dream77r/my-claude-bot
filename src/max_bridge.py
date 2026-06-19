"""
MaxBridge — мост агента в мессенджер МАКС (VK), второй транспорт рядом с Telegram.

Зеркалит роль `TelegramBridge`, но поверх SDK `maxapi` (aiogram-подобный
`Bot` + `Dispatcher` + long polling). Включается опционально: только если у
агента задан `max_bot_token` (см. `Agent._resolve_max_bot_token`). Если
`maxapi` не установлен — мост не поднимается, агент остаётся в Telegram.

Контракт шины (тот же, что у Telegram):
- inbound: публикуем `FleetMessage(target="agent:{name}",
  metadata["transport"]="max")` — `AgentWorker` затем маршрутизирует ответ
  обратно в `max:{name}`, потому что `transport=max` (см. agent_worker.py).
- outbound: слушаем очередь `max:{name}` и пересылаем пользователю только
  терминальные user-facing сообщения (`msg_type == OUTBOUND`). Стриминговые
  SYSTEM-события (processing_started / tool_use / text_delta) в v1 опускаем —
  в МАКС шлём один финальный ответ без live-редактирования.

v1-объём: текст в обе стороны + контроль доступа (allowed_users) + логирование
в memory/. Вложения (исходящие файлы из outbox) — best-effort: реальная
загрузка медиа в МАКС появится после сверки media-API `maxapi` на живом токене;
до тех пор шлём текстовую пометку и чистим outbox, чтобы не зациклить пересылку.
"""

from __future__ import annotations

import asyncio
import logging

from . import memory
from .bus import FleetBus, FleetMessage, MessageType
from .file_handler import clear_outbox

# Enum действий отправителя (TYPING_ON / MARK_SEEN). maxapi — опциональная
# зависимость: импорт под guard, чтобы модуль грузился и без SDK (в тестах).
try:
    from maxapi.enums import SenderAction as _SenderAction
except Exception:  # ImportError или сбой импорта SDK
    _SenderAction = None

# TYPING_ON в МАКС гаснет сам через ~5с. Фоновый таймер пере-шлёт его каждые
# TYPING_LOOP_SEC, чтобы «печатает…» держалось весь ход — включая «молчаливую»
# фазу размышления, когда от агента ещё нет ни текста, ни tool-событий
# (именно там индикатор гас и выглядел как зависание).
TYPING_LOOP_SEC = 3.0
# Предохранитель: максимум итераций фонового таймера (~5 мин), чтобы он не
# крутился вечно, если финальный ответ почему-то не придёт.
TYPING_MAX_ITERS = 100

logger = logging.getLogger(__name__)

# Лимит длины текстового сообщения МАКС (с запасом). Длинные ответы режем.
MAX_TEXT_LIMIT = 4000


def _dig(obj: object, *paths: str):
    """Вернуть первое непустое значение по списку точечных путей атрибутов.

    Структура события `maxapi` (event.message.body.text и т.п.) может слегка
    отличаться между версиями SDK — поэтому достаём поля устойчиво, пробуя
    несколько путей, вместо жёсткой привязки к одному.
    """
    for path in paths:
        cur = obj
        for attr in path.split("."):
            cur = getattr(cur, attr, None)
            if cur is None:
                break
        if cur is not None:
            return cur
    return None


def _split_text(text: str, limit: int = MAX_TEXT_LIMIT) -> list[str]:
    """Порезать длинный текст на куски <= limit, по возможности по переносам строк."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


class MaxBridge:
    """Per-agent мост в МАКС. Один экземпляр на агента, живёт в общем asyncio-loop."""

    def __init__(
        self,
        agent,
        semaphore: asyncio.Semaphore,
        *,
        bus: FleetBus,
        agent_worker=None,
        fleet_runtime=None,
    ):
        self.agent = agent
        self.semaphore = semaphore
        self.bus = bus
        self.agent_worker = agent_worker
        self.fleet_runtime = fleet_runtime
        self._bot = None
        self._dp = None
        self._running = False
        # chat_id → фоновая задача, держащая «печатает…» живым на время хода
        self._typing_tasks: dict[int, asyncio.Task] = {}

    # ── Lifecycle ──

    async def run(self) -> None:
        """Запустить МАКС-бота: long polling + bus listener. Блокирует до отмены."""
        try:
            from maxapi import Bot, Dispatcher
            from maxapi.types import MessageCreated
        except ImportError as e:
            logger.error(
                f"MaxBridge '{self.agent.name}': maxapi не установлен ({e}). "
                "Транспорт МАКС отключён."
            )
            return

        self._running = True
        bot = Bot(self.agent.max_bot_token)
        dp = Dispatcher()
        self._bot = bot
        self._dp = dp

        @dp.message_created()
        async def _on_message(event: MessageCreated):  # noqa: ANN001
            try:
                await self._handle_incoming(event)
            except Exception as exc:
                logger.error(
                    f"MaxBridge '{self.agent.name}' inbound error: {exc}"
                )

        listener_task = asyncio.create_task(self._bus_listener())
        logger.info(f"MAX-бот '{self.agent.name}' начал polling")
        try:
            await dp.start_polling(bot)
        except asyncio.CancelledError:
            logger.info(f"MAX-бот '{self.agent.name}' останавливается...")
            raise
        finally:
            self._running = False
            listener_task.cancel()
            for chat_id in list(self._typing_tasks):
                self._stop_typing(chat_id)
            await self._close_bot(bot)

    @staticmethod
    async def _close_bot(bot) -> None:
        """Закрыть сессию бота при остановке.

        В maxapi 1.2.0 метод — `close_session`; держим запасные имена на случай
        смены API в будущих версиях SDK.
        """
        for closer in ("close_session", "session.close", "close"):
            target = _dig(bot, closer)
            if callable(target):
                try:
                    res = target()
                    if asyncio.iscoroutine(res):
                        await res
                    return
                except Exception:
                    pass

    # ── Inbound: МАКС → bus → AgentWorker ──

    async def _handle_incoming(self, event) -> None:
        """Входящее сообщение из МАКС → публикуем в шину как INBOUND с transport=max."""
        text = _dig(event, "message.body.text", "message.text", "body.text", "text")
        chat_id_raw = _dig(
            event,
            "message.recipient.chat_id",
            "chat_id",
            "message.chat_id",
            "recipient.chat_id",
        )
        user_id_raw = _dig(
            event,
            "message.sender.user_id",
            "message.sender.id",
            "from_user.id",
            "sender.user_id",
        )

        if chat_id_raw is None:
            logger.warning(
                f"MaxBridge '{self.agent.name}': не нашёл chat_id во входящем "
                "событии — пропускаю"
            )
            return
        try:
            chat_id = int(chat_id_raw)
        except (TypeError, ValueError):
            logger.warning(
                f"MaxBridge '{self.agent.name}': невалидный chat_id={chat_id_raw!r}"
            )
            return

        if not text or not str(text).strip():
            logger.info(
                f"MaxBridge '{self.agent.name}': пустой текст — пропуск "
                "(не-текстовое сообщение?)"
            )
            return  # v1: обрабатываем только текст
        text = str(text)
        logger.info(
            f"MaxBridge '{self.agent.name}': входящее от user={user_id_raw} "
            f"chat={chat_id_raw}"
        )

        # Контроль доступа: повторяем семантику Telegram (allowed_users).
        # Fail-closed: если список ограничен, а отправителя определить не
        # удалось — отклоняем (безопаснее, чем пускать неизвестного).
        user_id = None
        if user_id_raw is not None:
            try:
                user_id = int(user_id_raw)
            except (TypeError, ValueError):
                user_id = None
        if self.agent.allowed_users:
            if user_id is None:
                logger.warning(
                    f"MaxBridge '{self.agent.name}': не определил отправителя при "
                    "заданном allowed_users — отклоняю сообщение"
                )
                return
            if not self.agent.is_user_allowed(user_id):
                logger.info(
                    f"MaxBridge '{self.agent.name}': доступ запрещён user={user_id}"
                )
                return

        # Лог входящего в память (как делает Telegram-мост на early-persist).
        try:
            memory.log_message(self.agent.agent_dir, "user", text)
        except Exception as exc:
            logger.error(f"MaxBridge log_message(user) error: {exc}")

        await self.bus.publish(FleetMessage(
            source=f"max:{chat_id}",
            target=f"agent:{self.agent.name}",
            content=text,
            msg_type=MessageType.INBOUND,
            chat_id=chat_id,
            user_id=user_id or 0,
            metadata={"transport": "max"},
        ))

    # ── Outbound: bus (max:{name}) → МАКС ──

    async def _bus_listener(self) -> None:
        """Слушать очередь max:{name} и пересылать ответы агента в МАКС."""
        queue_name = f"max:{self.agent.name}"
        self.bus.subscribe(queue_name)  # идемпотентно (main уже подписал)
        logger.info(f"MAX bus listener запущен для '{queue_name}'")
        while True:
            try:
                msg = await self.bus.consume(queue_name)
                await self._dispatch_outbound(msg)
            except asyncio.CancelledError:
                logger.info(f"MAX bus listener '{queue_name}' остановлен")
                break
            except KeyError:
                # Подписка ещё не зарегистрирована — короткая пауза и повтор.
                await asyncio.sleep(0.5)
            except Exception as exc:
                logger.error(f"MAX bus listener error ({self.agent.name}): {exc}")

    async def _dispatch_outbound(self, msg: FleetMessage) -> None:
        """Обработать одно сообщение из шины для МАКС.

        - SYSTEM-события хода (processing_started / tool_use / text_delta) →
          индикаторы «прочитано» + «печатает…» (без live-редактирования текста).
          Так пользователь сразу видит, что сообщение принято и бот работает.
        - OUTBOUND (финальный ответ, ошибки, уведомления) → отправляем текст
          (и файлы) в чат.
        """
        chat_id = msg.chat_id
        event = msg.metadata.get("event", "")

        # SYSTEM: на старте хода — «прочитано» + запуск фонового «печатает…».
        # Прочие SYSTEM-события (tool_use/text_delta/queued) игнорим: индикатор
        # держит фоновый таймер, а не отдельные события.
        if msg.msg_type == MessageType.SYSTEM:
            if chat_id and event == "processing_started":
                await self._send_seen(chat_id)
                self._start_typing(chat_id)
            return

        if msg.msg_type != MessageType.OUTBOUND:
            return
        if not chat_id:
            logger.warning(
                f"MaxBridge '{self.agent.name}': outbound без chat_id — пропуск"
            )
            return

        self._stop_typing(chat_id)  # ход завершён — гасим «печатает…»

        text = msg.content or ""
        if text.strip():
            await self._send_text(chat_id, text)
            try:
                memory.log_message(self.agent.agent_dir, "assistant", text)
            except Exception as exc:
                logger.error(f"MaxBridge log_message(assistant) error: {exc}")

        if msg.files:
            await self._send_files(chat_id, msg.files)
            # Чистим outbox даже при текстовом fallback: ответ на МАКС-turn
            # маршрутизируется ТОЛЬКО в max:{name}, Telegram-мост его не видит
            # и outbox не очистит — иначе те же файлы прицепятся к след. ответу.
            agent_dir = msg.metadata.get("agent_dir")
            if agent_dir:
                clear_outbox(agent_dir)

    async def _send_seen(self, chat_id: int) -> None:
        """Отметить входящее как прочитанное (read-receipt)."""
        if self._bot is None or _SenderAction is None:
            return
        try:
            await self._bot.send_action(
                chat_id=chat_id, action=_SenderAction.MARK_SEEN
            )
        except Exception as exc:
            logger.debug(f"MaxBridge send_action(MARK_SEEN) error: {exc}")

    def _start_typing(self, chat_id: int) -> None:
        """Запустить фоновый таймер «печатает…» для чата (заменяя прежний)."""
        self._stop_typing(chat_id)
        if self._bot is None or _SenderAction is None:
            return
        self._typing_tasks[chat_id] = asyncio.create_task(
            self._typing_loop(chat_id)
        )

    def _stop_typing(self, chat_id: int) -> None:
        """Остановить фоновый таймер «печатает…» для чата."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: int) -> None:
        """Держать «печатает…» живым: TYPING_ON каждые TYPING_LOOP_SEC.

        Первый TYPING_ON шлётся сразу, затем — пока ход не завершится (таймер
        отменяют в _stop_typing) или пока не упрёмся в TYPING_MAX_ITERS.
        """
        try:
            for _ in range(TYPING_MAX_ITERS):
                try:
                    await self._bot.send_action(
                        chat_id=chat_id, action=_SenderAction.TYPING_ON
                    )
                except Exception as exc:
                    logger.debug(f"MaxBridge send_action(TYPING_ON) error: {exc}")
                await asyncio.sleep(TYPING_LOOP_SEC)
        except asyncio.CancelledError:
            pass

    async def _send_text(self, chat_id: int, text: str) -> None:
        """Отправить текст в чат МАКС (с разбивкой длинных сообщений)."""
        for part in _split_text(text):
            try:
                await self._bot.send_message(chat_id=chat_id, text=part)
            except Exception as exc:
                logger.error(
                    f"MaxBridge '{self.agent.name}' send_message error "
                    f"(chat={chat_id}): {exc}"
                )

    async def _send_files(self, chat_id: int, files: list[str]) -> None:
        """Отправить исходящие файлы в МАКС.

        v1 — best-effort: media-API `maxapi` (InputMedia/upload) не сверен на
        живом токене, поэтому пока шлём текстовую пометку с именами файлов и
        не пытаемся гадать сигнатуру загрузки. ЕДИНАЯ точка интеграции: когда
        media-API подтверждён, реальную загрузку вкладываем сюда.
        """
        import os

        names = [os.path.basename(f) for f in files if f]
        if not names:
            return
        listing = "\n".join(f"• {n}" for n in names)
        note = (
            "📎 Подготовил файл(ы):\n"
            f"{listing}\n\n"
            "_(отправка вложений в МАКС появится в следующей версии моста)_"
        )
        await self._send_text(chat_id, note)
        logger.info(
            f"MaxBridge '{self.agent.name}': {len(names)} файл(ов) — текстовая "
            "пометка (вложения в МАКС ещё не реализованы)"
        )
