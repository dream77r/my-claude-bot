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
- outbound: слушаем очередь `max:{name}`. Стриминговые SYSTEM-события
  (`processing_started` / `text_delta`) превращаем в живой статус-ответ —
  одно сообщение, дописываемое по мере генерации через `edit_message`
  (зеркало `StatusMessage` из Telegram). Терминальное `OUTBOUND`
  (финальный ответ/ошибка) финализирует этот статус на месте.

v1-объём: текст в обе стороны + контроль доступа (allowed_users) + логирование
в memory/ + ЖИВОЙ стриминг ответа (status-message edit) + РЕАЛЬНАЯ отправка
файлов-вложений (upload_media → send_message(attachments=...)). Tool-события
(`tool_use`) в МАКС не выводим — статус-сообщение «⏳ Думаю…» + индикатор
«печатает…» уже показывают, что бот работает.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from . import memory
from .bus import FleetBus, FleetMessage, MessageType
from .file_handler import clear_outbox

# maxapi — ОПЦИОНАЛЬНАЯ зависимость: все её импорты под guard, чтобы модуль
# грузился и без SDK (в тестах). Enum действий отправителя (TYPING_ON /
# MARK_SEEN):
try:
    from maxapi.enums import SenderAction as _SenderAction
except Exception:  # ImportError или сбой импорта SDK
    _SenderAction = None

# Класс-обёртка медиафайла для загрузки (Задача A). `InputMedia(path, type=...)`
# с явным type НЕ читает файл при конструировании (тип берём по расширению),
# поэтому helper остаётся чистым и тестируемым без SDK.
try:
    from maxapi.types import InputMedia as _InputMedia
except Exception:  # ImportError или сбой импорта SDK
    _InputMedia = None

# TYPING_ON в МАКС гаснет сам через ~5с. Фоновый таймер пере-шлёт его каждые
# TYPING_LOOP_SEC, чтобы «печатает…» держалось весь ход — включая «молчаливую»
# фазу размышления, когда от агента ещё нет ни текста, ни tool-событий
# (именно там индикатор гас и выглядел как зависание). Работает ПАРАЛЛЕЛЬНО со
# статус-сообщением: typing — анимация в шапке чата, статус — текст в ленте;
# они не конфликтуют и не мигают (разные элементы UI).
TYPING_LOOP_SEC = 3.0
# Предохранитель: максимум итераций фонового таймера (~5 мин), чтобы он не
# крутился вечно, если финальный ответ почему-то не придёт.
TYPING_MAX_ITERS = 100

# Минимальный интервал между edit-запросами статус-сообщения при стриминге.
# edit_message в МАКС имеет рейт-лимиты → троттлим по времени (~раз в 1.2с).
STREAM_EDIT_INTERVAL = 1.2

logger = logging.getLogger(__name__)

# Лимит длины текстового сообщения МАКС (с запасом). Длинные ответы режем.
MAX_TEXT_LIMIT = 4000

# Сопоставление расширения файла → UploadType МАКС (значения совпадают со
# строковыми значениями enum `maxapi.enums.UploadType`: image/video/audio/file,
# поэтому helper не зависит от самого SDK и тестируется без него).
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic",
               ".heif", ".tif", ".tiff", ".svg"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp", ".flv",
               ".wmv", ".mpeg", ".mpg"}
_AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".aac", ".flac",
               ".opus", ".wma"}


def _upload_type_for(path: str) -> str:
    """Определить тип вложения МАКС по расширению файла.

    Возвращает строку, совпадающую со значением `maxapi.enums.UploadType`
    (`image`/`video`/`audio`/`file`). По умолчанию — `file` (документ).
    `InputMedia` принимает строковый type и сам валидирует его, поэтому
    эту чистую функцию можно тестировать без установленного SDK.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "file"


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


def _extract_mid(sent) -> str | None:
    """Достать message_id (mid) из ответа `send_message` (SendedMessage).

    Структура: `SendedMessage.message.body.mid` (строка). Достаём устойчиво
    через `_dig`, чтобы пережить мелкие отличия между версиями SDK.
    """
    mid = _dig(sent, "message.body.mid", "body.mid", "message.mid", "mid")
    return str(mid) if mid else None


class _MaxStatus:
    """Живой статус-ответ в МАКС: одно сообщение, дописываемое по мере генерации.

    Зеркалит `StatusMessage` из Telegram, но проще: МАКС в v1 шлёт plain-text,
    поэтому только троттленный `edit_message` без rich/HTML-веток. Жизненный
    цикл одного хода:
      • `start()`  — отправить стартовое «⏳ Думаю…», запомнить message_id;
      • `show()`   — троттленно дописывать накопленный текст (text_delta);
      • `finalize()` — вписать финальный текст на место (без нового сообщения),
                       вернув «хвост» (куски сверх лимита) для досылки.
    """

    def __init__(self, bot, chat_id: int, *, limit: int = MAX_TEXT_LIMIT,
                 throttle: float = STREAM_EDIT_INTERVAL):
        self.bot = bot
        self.chat_id = chat_id
        self.limit = limit
        self.throttle = throttle
        self.message_id: str | None = None
        self._last_edit: float = 0.0
        self._last_text: str = ""

    async def start(self, text: str) -> bool:
        """Отправить стартовое статус-сообщение. True — удалось (есть message_id).

        Любой сбой (бот недоступен, send_message упал, mid не извлёкся) →
        возвращаем False, и мост деградирует к поведению без live-edit.
        """
        if self.bot is None:
            return False
        try:
            sent = await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception as exc:
            logger.debug(f"MaxStatus start send_message error: {exc}")
            return False
        mid = _extract_mid(sent)
        if not mid:
            logger.debug("MaxStatus start: не извлёк message_id из ответа")
            return False
        self.message_id = mid
        self._last_text = text
        self._last_edit = time.monotonic()
        return True

    async def show(self, text: str) -> None:
        """Троттленно обновить статус накопленным текстом (для text_delta)."""
        if not self.message_id:
            return
        preview = text[: self.limit]
        now = time.monotonic()
        if now - self._last_edit < self.throttle:
            return  # троттл: пропускаем — финал всё равно допишет полный текст
        if preview == self._last_text:
            return  # «message is not modified» — нечего слать
        self._last_text = preview
        self._last_edit = now
        try:
            await self.bot.edit_message(message_id=self.message_id, text=preview)
        except Exception as exc:
            logger.debug(f"MaxStatus show edit_message error: {exc}")

    async def finalize(self, text: str) -> list[str]:
        """Финализировать статус финальным текстом (edit на месте).

        Возвращает список «хвостовых» кусков, которые НЕ влезли в первое
        сообщение и которые caller должен дослать новыми сообщениями
        (пусто, если всё уместилось). Если статуса нет или edit упал —
        возвращаем ВЕСЬ текст кусками, чтобы caller отправил его заново.
        """
        parts = _split_text(text, self.limit) or [""]
        if not self.message_id:
            return parts
        # Если статус уже показывает ровно этот первый кусок (короткий ответ
        # успел отстримиться через show() до финала — text_delta несёт ПОЛНЫЙ
        # накопленный текст), повторный identical-edit вернёт «message is not
        # modified» (ошибку), и caller продублирует ответ новым сообщением.
        # Такой no-op edit пропускаем: текст уже на месте.
        if parts[0] == self._last_text:
            return parts[1:]
        try:
            await self.bot.edit_message(
                message_id=self.message_id, text=parts[0]
            )
        except Exception as exc:
            logger.warning(f"MaxStatus finalize edit_message error: {exc}")
            return parts  # edit не удался → пусть caller дошлёт всё новыми
        self._last_text = parts[0]
        return parts[1:]


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
        # chat_id → живое статус-сообщение текущего хода (live-стриминг ответа)
        self._status_messages: dict[int, _MaxStatus] = {}

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
            self._status_messages.clear()
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

        SYSTEM-события хода → живой статус-ответ + индикаторы:
          • `processing_started` → «прочитано» + старт «печатает…» + отправка
            стартового статус-сообщения «⏳ Думаю…» (его потом дописываем).
          • `text_delta` → троттленный edit статус-сообщения накопленным текстом.
          • прочие (`tool_use` и т.п.) → игнор (статус + typing уже показывают
            работу; tool-подсказки в v1 в МАКС не выводим).
        OUTBOUND (финальный ответ/ошибка) → финализируем статус на месте
        (edit), длинный хвост/файлы досылаем новыми сообщениями.
        """
        chat_id = msg.chat_id
        event = msg.metadata.get("event", "")

        if msg.msg_type == MessageType.SYSTEM:
            if not chat_id:
                return
            if event == "processing_started":
                await self._send_seen(chat_id)
                self._start_typing(chat_id)
                await self._begin_status(chat_id)
            elif event == "text_delta":
                await self._stream_status(chat_id, msg.content or "")
            return

        if msg.msg_type != MessageType.OUTBOUND:
            return
        if not chat_id:
            logger.warning(
                f"MaxBridge '{self.agent.name}': outbound без chat_id — пропуск"
            )
            return

        self._stop_typing(chat_id)  # ход завершён — гасим «печатает…»
        status = self._status_messages.pop(chat_id, None)

        text = msg.content or ""
        has_text = bool(text.strip())

        # Финализация ответа. Если есть живой статус — дописываем финальный текст
        # ПРЯМО в него (edit на месте, без нового сообщения); «хвост» сверх лимита
        # уходит новыми сообщениями. Если статуса нет (старт упал/обычный
        # OUTBOUND без хода) — просто шлём текст с разбивкой.
        if status is not None and status.message_id:
            # Чем финализировать статус: текстом ответа, иначе — пометкой про
            # файлы, иначе — нейтральным «готово» (пустой ответ без файлов).
            body = text if has_text else (
                "📎 Вложения ниже" if msg.files else "✅ Готово"
            )
            remainder = await status.finalize(body)
            for chunk in remainder:
                await self._send_chunk(chat_id, chunk)
        elif has_text:
            await self._send_text(chat_id, text)

        if has_text:
            try:
                memory.log_message(self.agent.agent_dir, "assistant", text)
            except Exception as exc:
                logger.error(f"MaxBridge log_message(assistant) error: {exc}")

        if msg.files:
            # Инвариант: outbox чистим ПОСЛЕ отправки (успех ИЛИ ошибка). Ответ
            # на МАКС-turn маршрутизируется ТОЛЬКО в max:{name} — Telegram-мост
            # его не видит и outbox не очистит, иначе те же файлы прицепятся к
            # следующему ответу. try/finally гарантирует очистку даже при сбое.
            try:
                await self._send_files(chat_id, msg.files)
            finally:
                agent_dir = msg.metadata.get("agent_dir")
                if agent_dir:
                    clear_outbox(agent_dir)

    # ── Live-статус (стриминг ответа через edit_message) ──

    async def _begin_status(self, chat_id: int) -> None:
        """Отправить стартовое статус-сообщение «⏳ Думаю…» и запомнить его.

        Fallback-устойчивость: если отправка статуса не удалась — просто не
        заводим live-edit (typing-таймер уже запущен и покажет активность),
        а финальный ответ уйдёт обычным сообщением.
        """
        if self._bot is None:
            return
        # Подчистить «висячий» статус от прошлого хода в этом чате, если он не
        # был финализирован (редкий случай: ход отменили — терминального
        # OUTBOUND не пришло, pop живёт только на OUTBOUND-пути). Иначе старое
        # «⏳ Думаю…» осталось бы навсегда, а запись подтекала бы. typing уже
        # заменён в _start_typing; здесь закрываем именно статус-сообщение.
        stale = self._status_messages.pop(chat_id, None)
        if stale is not None and stale.message_id:
            try:
                await stale.finalize("⏸ (прервано)")
            except Exception as exc:
                logger.debug(f"MaxBridge stale status cleanup error: {exc}")
        status = _MaxStatus(self._bot, chat_id)
        if await status.start("⏳ Думаю…"):
            self._status_messages[chat_id] = status

    async def _stream_status(self, chat_id: int, text: str) -> None:
        """Дописать накопленный текст в статус-сообщение (троттленно)."""
        if not text.strip():
            return
        status = self._status_messages.get(chat_id)
        if status is not None:
            await status.show(text)

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
            await self._send_chunk(chat_id, part)

    async def _send_chunk(self, chat_id: int, text: str) -> None:
        """Отправить один (уже укладывающийся в лимит) кусок текста."""
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            logger.error(
                f"MaxBridge '{self.agent.name}' send_message error "
                f"(chat={chat_id}): {exc}"
            )

    async def _send_files(self, chat_id: int, files: list[str]) -> None:
        """Отправить исходящие файлы в МАКС реальными вложениями.

        Для каждого файла: определяем тип по расширению → `InputMedia` →
        `upload_media` (получаем attachment с токеном) → `send_message(
        attachments=[...])`. Если SDK media-API недоступен (maxapi не
        установлен) или загрузка/отправка упала — деградируем к текстовой
        пометке с именем файла, чтобы пользователь хотя бы знал о вложении.
        Метод НИКОГДА не пробрасывает исключение наружу — инвариант очистки
        outbox в `_dispatch_outbound` (try/finally) от этого не зависит.
        """
        for fpath in files:
            if not fpath:
                continue
            name = os.path.basename(fpath)
            attachment = await self._upload_attachment(fpath)
            if attachment is not None:
                try:
                    await self._bot.send_message(
                        chat_id=chat_id, attachments=[attachment]
                    )
                    logger.info(
                        f"MaxBridge '{self.agent.name}': вложение отправлено "
                        f"({name})"
                    )
                    continue
                except Exception as exc:
                    logger.error(
                        f"MaxBridge '{self.agent.name}' send_message(attachments) "
                        f"error ({name}): {exc}"
                    )
            # Fallback: не смогли отправить вложением — хотя бы текстовая пометка.
            await self._send_chunk(
                chat_id, f"📎 {name}\n(не удалось отправить вложением)"
            )

    async def _upload_attachment(self, fpath: str):
        """Загрузить файл в МАКС и вернуть attachment для `send_message`.

        Возвращает `AttachmentUpload` либо None, если media-API недоступен
        (maxapi не установлен) или загрузка не удалась — тогда caller
        деградирует к текстовой пометке. Тип вложения берём по расширению.

        Устойчивость: фото-/медиа-сервер МАКС может не принять конкретный файл
        (напр. битую/вырожденную картинку — в ответе upload-сервера не будет
        `photos`). Поэтому при сбое «правильного» типа ретраим один раз как
        обычный документ (`type=file`): простой file-эндпоинт берёт любой файл,
        и пользователь получит вложение, пусть и как документ, а не пометку.
        Сверено на живом токене: и `file`, и `image` для нормальных файлов ок.
        """
        if self._bot is None or _InputMedia is None:
            return None
        primary = _upload_type_for(fpath)
        attempts = [primary] if primary == "file" else [primary, "file"]
        for upload_type in attempts:
            try:
                media = _InputMedia(fpath, type=upload_type)
                return await self._bot.upload_media(media)
            except Exception as exc:
                logger.error(
                    f"MaxBridge '{self.agent.name}' upload_media error "
                    f"({os.path.basename(fpath)}, type={upload_type}): {exc}"
                )
        return None
