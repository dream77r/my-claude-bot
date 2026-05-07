"""
Message handler & buffering mixin.

Входящие сообщения пользователя: текст, документ, фото, голосовое.
Buffer aggregation (`_add_to_buffer` + `_flush_buffer`) собирает соседние
сообщения за `MESSAGE_BUFFER_DELAY` секунд и отправляет в Claude одним
запросом — это и снижает quota, и даёт связный контекст.

Mixin предполагает наличие на self: `agent`, `bus`, `semaphore`, `_buffers`,
`_thread_ids`, `_user_ids`, `_status_messages`, `_active_tasks`,
`_wizard_state`, `_pending_group_setups`, `_pending_topic_setups`,
а также методов `_check_auth`, `_is_group_chat`, `_is_bot_mentioned`,
`_is_topic_allowed`, `_get_sender_name`, `_effective_dir`, `_lang`,
`_group_onboarding`, `_get_master_agent_dir`, `_wizard_handle_input`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import Application, ContextTypes

from .. import memory
from ..file_handler import clear_outbox, download_file, send_file
from ..formatter import TG_MESSAGE_LIMIT
from ..i18n import t
from ..status_message import StatusMessage
from ..voice_handler import download_voice, get_deepgram_api_key, transcribe

if TYPE_CHECKING:
    from ..telegram_bridge import TelegramBridge  # noqa: F401

logger = logging.getLogger(__name__)

# Импорт буфера-задержки берём из telegram_bridge через ленивый getattr,
# чтобы не плодить дубли констант (она нужна только тут).


class MessageHandlerMixin:
    """
    Хэндлеры входящих сообщений + message aggregation buffer.
    """

    async def _flush_buffer(
        self: TelegramBridge,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Отправить накопленные сообщения в Claude после задержки."""
        from ..telegram_bridge import MESSAGE_BUFFER_DELAY, send_long_message

        await asyncio.sleep(MESSAGE_BUFFER_DELAY)

        if chat_id not in self._buffers:
            return

        messages, files, _ = self._buffers.pop(chat_id)

        if not messages and not files:
            return

        combined = "\n".join(messages)

        # Определить, является ли чат групповым (по знаку chat_id — группы отрицательные)
        is_group = chat_id < 0

        # Thread ID для топиков
        thread_id = self._thread_ids.pop(chat_id, None)

        # Определить директорию пользователя (per-user в multi_user mode)
        user_id = self._user_ids.pop(chat_id, None)
        if self.agent.is_multi_user and user_id:
            user_dir = self.agent.get_effective_dir(user_id)
        else:
            user_dir = self.agent.agent_dir

        # Если онбординг не пройден — добавить инструкцию сохранить профиль (только DM)
        if not is_group and memory.is_onboarding_needed(user_dir) and not memory.is_onboarding_done(user_dir):
            lang = self._lang()
            combined = combined + t("onboarding_save_instruction", lang)
            memory.mark_onboarding_done(user_dir)

        # Входящие сообщения уже залогированы в _handle_text/_handle_document/
        # _handle_photo/_handle_voice как early persist — до буферизации.
        # Тут не дублируем.

        # ── Режим MessageBus ──
        if self.bus:
            from ..bus import FleetMessage, MessageType

            # Убрать предыдущий статус если остался
            old_status = self._status_messages.pop(chat_id, None)
            if old_status:
                await old_status.cleanup()

            # Показать статус с таймером — первый tool hint заменит его (master)
            status = StatusMessage(chat_id, context, thread_id)
            await status.show("💬 Думаю...")
            status.start_typing()
            status.start_thinking_timer()
            self._status_messages[chat_id] = status

            # Опубликовать в bus → orchestrator → agent_worker
            # target указываем напрямую на агента (каждый бот = свой агент)
            metadata = {}
            if is_group:
                metadata["group_chat_id"] = chat_id
            if thread_id:
                metadata["message_thread_id"] = thread_id
            await self.bus.publish(FleetMessage(
                source=f"telegram:{chat_id}",
                target=f"agent:{self.agent.name}",
                content=combined,
                msg_type=MessageType.INBOUND,
                chat_id=chat_id,
                user_id=0,
                files=files,
                metadata=metadata,
            ))
            return

        # ── Fallback: прямой вызов (без bus) ──
        old_status = self._status_messages.pop(chat_id, None)
        if old_status:
            await old_status.cleanup()

        status = StatusMessage(chat_id, context, thread_id)
        await status.show("💬 Думаю...")
        status.start_typing()
        status.start_thinking_timer()

        task = asyncio.current_task()
        self._active_tasks[chat_id] = task

        async def _on_text_delta(text: str) -> None:
            status.stop_thinking_timer()
            preview = text[:TG_MESSAGE_LIMIT - 20] + ("\n..." if len(text) > TG_MESSAGE_LIMIT - 20 else "")
            await status.show(preview, streaming=True)

        async def _on_tool_use(hint: str) -> None:
            # Master-агент показывает tool hints, worker — нет
            if self.agent.is_master:
                status.stop_thinking_timer()
                await status.show(f"⏳ {hint}")

        try:
            response = await self.agent.call_claude(
                combined,
                files or None,
                self.semaphore,
                on_tool_use=_on_tool_use,
                on_text_delta=_on_text_delta,
                group_chat_id=chat_id if is_group else None,
                user_id=user_id,
            )

            # В группах ответ логируется в groups/{chat_id}/daily/;
            # в персональный daily пишем только DM-ответы (симметрично ~2142).
            if not is_group:
                memory.log_message(user_dir, "assistant", response)

            # Проверить outbox файлы
            from ..file_handler import scan_outbox
            outbox_files = scan_outbox(user_dir)

            # Финализация: edit на месте если коротко и нет файлов
            finalized = False
            if not outbox_files:
                finalized = await status.finalize(response)
            else:
                await status.cleanup()

            # Если edit не удался — отправить новым сообщением
            if not finalized:
                await send_long_message(
                    chat_id, response, context, message_thread_id=thread_id
                )

            # Отправить файлы из outbox
            if outbox_files:
                for fpath in outbox_files:
                    try:
                        await send_file(context.bot, chat_id, fpath, message_thread_id=thread_id)
                    except Exception as fe:
                        logger.error(f"Outbox send error: {fe}")
                clear_outbox(user_dir)

        except asyncio.CancelledError:
            await status.cleanup()
            logger.info(f"Request cancelled for chat {chat_id}")
        except asyncio.TimeoutError:
            await status.cleanup()
            await context.bot.send_message(
                chat_id=chat_id,
                text="Ответ занял слишком долго. Попробуй переформулировать.",
            )
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            await status.cleanup()
            await context.bot.send_message(
                chat_id=chat_id,
                text="Произошла ошибка. Попробуй ещё раз.",
            )
        finally:
            self._active_tasks.pop(chat_id, None)

    def _add_to_buffer(
        self: TelegramBridge,
        chat_id: int,
        text: str,
        file_path: str | None,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int | None = None,
    ) -> None:
        """Добавить сообщение в буфер и (пере)запустить таймер."""
        if chat_id in self._buffers:
            messages, files, task = self._buffers[chat_id]
            task.cancel()
        else:
            messages = []
            files = []

        if text:
            messages.append(text)
        if file_path:
            files.append(file_path)

        if user_id is not None:
            self._user_ids[chat_id] = user_id

        flush_task = asyncio.create_task(
            self._flush_buffer(chat_id, context)
        )
        self._buffers[chat_id] = (messages, files, flush_task)

    # ── Хэндлеры сообщений ──

    async def _handle_text(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Обработать текстовое сообщение."""
        if not self._check_auth(update):
            return

        text = update.message.text
        if not text or not text.strip():
            return

        chat_id = update.effective_chat.id
        is_group = self._is_group_chat(update)

        # Перехват визарда создания агента (только в DM)
        if not is_group and chat_id in self._wizard_state:
            await self._wizard_handle_input(update, context, text)
            return

        # Перехват ответа владельца на настройку группы (только в DM)
        if not is_group and chat_id in self._pending_group_setups:
            group_chat_id = self._pending_group_setups.pop(chat_id)
            memory.update_group_rules(self.agent.agent_dir, group_chat_id, text)
            await update.message.reply_text(
                f"Готово! Правила для группы сохранены. "
                f"Буду вести себя согласно инструкции."
            )
            return

        # Захватить thread_id (топики)
        thread_id = getattr(update.message, "message_thread_id", None)

        # Тихое логирование в группах — КАЖДОЕ сообщение, даже без mention
        if is_group:
            sender = self._get_sender_name(update)
            topic_tag = f" [тема:{thread_id}]" if thread_id else ""
            memory.log_group_message(
                self.agent.agent_dir, chat_id, sender, text + topic_tag
            )
            # Групповой онбординг при первом сообщении
            if memory.is_group_onboarding_needed(self.agent.agent_dir, chat_id):
                await self._group_onboarding(update, context)

            if not self._is_bot_mentioned(update, context):
                return  # Молча ушёл, но сообщение УЖЕ залогировано

            # Pending topic setup — владелец упомянул бота в нужном топике
            if chat_id in self._pending_topic_setups and thread_id:
                owner_id = self._pending_topic_setups.get(chat_id)
                user = update.effective_user
                if user and user.id == owner_id:
                    self._pending_topic_setups.pop(chat_id)
                    memory.set_group_setting(
                        self.agent.agent_dir, chat_id, "allowed_topic", thread_id
                    )
                    try:
                        await context.bot.send_message(
                            chat_id=owner_id,
                            text=f"Запомнил! Буду отвечать только в этой теме (ID: {thread_id}).",
                        )
                    except Exception:
                        pass

            # Проверка топик-фильтра
            if not self._is_topic_allowed(chat_id, thread_id):
                return  # Бот ограничен другим топиком

            # Убрать @botname из текста
            bot_username = context.bot.username
            if bot_username:
                text = text.replace(f"@{bot_username}", "").strip()

        # Сохранить thread_id для ответа в правильном топике
        if thread_id:
            self._thread_ids[chat_id] = thread_id

        # Добавить имя отправителя (важно для групп).
        # Если сообщение — reply на сообщение бота, добавить контекст цитаты,
        # чтобы агент понимал на что отвечает.
        if is_group:
            sender_name = self._get_sender_name(update)
            msg = update.message
            reply = msg.reply_to_message if msg else None
            if (
                reply
                and reply.from_user
                and reply.from_user.id == context.bot.id
            ):
                original = (reply.text or reply.caption or "").strip()
                if original:
                    MAX_QUOTE = 100
                    quote = (
                        original[:MAX_QUOTE].rstrip() + "…"
                        if len(original) > MAX_QUOTE
                        else original
                    )
                    text = f"[{sender_name}] ↩️ «{quote}»:\n{text}"
                else:
                    text = f"[{sender_name}] ↩️ (ответил на сообщение бота):\n{text}"
            else:
                text = f"[{sender_name}]: {text}"

        # Early persist — сохраняем до буферизации/LLM-вызова.
        # Если процесс упадёт в буфере или в середине call_claude — сообщение
        # уже на диске. В группах лог выше (log_group_message в 2564).
        if not is_group:
            try:
                memory.log_message(
                    self._effective_dir(update), "user", text
                )
            except Exception as e:
                logger.error(f"early_persist text failed: {e}")

        self._add_to_buffer(chat_id, text, None, context, user_id=update.effective_user.id if update.effective_user else None)

    async def _handle_document(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Обработать файл."""
        if not self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        is_group = self._is_group_chat(update)
        doc = update.message.document
        caption = update.message.caption or f"Отправлен файл: {doc.file_name}"

        # Тихое логирование в группах
        if is_group:
            sender = self._get_sender_name(update)
            memory.log_group_message(
                self.agent.agent_dir, chat_id, sender, f"[файл: {doc.file_name}] {caption}"
            )
            if not self._is_bot_mentioned(update, context):
                return

        thread_id = getattr(update.message, "message_thread_id", None)
        if is_group and not self._is_topic_allowed(chat_id, thread_id):
            return
        if thread_id:
            self._thread_ids[chat_id] = thread_id

        # Проверка размера (20MB лимит)
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("Файл слишком большой (макс. 20MB).")
            return

        try:
            file_path = await download_file(
                context.bot, doc.file_id, self._effective_dir(update)
            )
            # Early persist (DM). В группах лог выше.
            if not is_group:
                try:
                    memory.log_message(
                        self._effective_dir(update), "user", caption, [file_path]
                    )
                except Exception as e:
                    logger.error(f"early_persist document failed: {e}")
            self._add_to_buffer(chat_id, caption, file_path, context, user_id=update.effective_user.id if update.effective_user else None)
        except Exception as e:
            logger.error(f"File download error: {e}")
            await update.message.reply_text("Не удалось скачать файл.")

    async def _handle_photo(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Обработать фото (берём наибольшее разрешение)."""
        if not self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        is_group = self._is_group_chat(update)
        caption = update.message.caption or "Отправлено фото"

        # Тихое логирование в группах
        if is_group:
            sender = self._get_sender_name(update)
            memory.log_group_message(
                self.agent.agent_dir, chat_id, sender, f"[фото] {caption}"
            )
            if not self._is_bot_mentioned(update, context):
                return

        thread_id = getattr(update.message, "message_thread_id", None)
        if is_group and not self._is_topic_allowed(chat_id, thread_id):
            return
        if thread_id:
            self._thread_ids[chat_id] = thread_id

        photo = update.message.photo[-1]  # Наибольшее разрешение

        try:
            file_path = await download_file(
                context.bot, photo.file_id, self._effective_dir(update)
            )
            # Early persist (DM). В группах лог выше.
            if not is_group:
                try:
                    memory.log_message(
                        self._effective_dir(update), "user", caption, [file_path]
                    )
                except Exception as e:
                    logger.error(f"early_persist photo failed: {e}")
            self._add_to_buffer(chat_id, caption, file_path, context, user_id=update.effective_user.id if update.effective_user else None)
        except Exception as e:
            logger.error(f"Photo download error: {e}")
            await update.message.reply_text("Не удалось скачать фото.")

    async def _handle_voice(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Обработать голосовое сообщение: скачать OGG, транскрибировать, отправить как текст."""
        if not self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        is_group = self._is_group_chat(update)

        # Тихое логирование в группах
        if is_group:
            sender = self._get_sender_name(update)
            memory.log_group_message(
                self.agent.agent_dir, chat_id, sender, "[голосовое сообщение]"
            )
            if not self._is_bot_mentioned(update, context):
                return

        thread_id = getattr(update.message, "message_thread_id", None)
        if is_group and not self._is_topic_allowed(chat_id, thread_id):
            return
        if thread_id:
            self._thread_ids[chat_id] = thread_id

        # Проверить что Deepgram настроен
        master_dir = self._get_master_agent_dir()
        if not get_deepgram_api_key(self.agent.agent_dir, master_dir):
            await update.message.reply_text(
                "Голосовые сообщения пока не настроены.\n"
                "Отправь мне ключ Deepgram API — и я включу распознавание голоса.\n"
                "Получить ключ: https://console.deepgram.com/"
            )
            return

        voice = update.message.voice or update.message.audio
        if not voice:
            return

        try:
            # Скачать OGG
            user_dir = self._effective_dir(update)
            ogg_path = await download_voice(
                context.bot, voice.file_id, user_dir
            )

            # Транскрибировать
            transcript = await transcribe(
                ogg_path,
                agent_dir=user_dir,
                master_agent_dir=master_dir,
            )

            # Добавить в буфер как текст (с пометкой что это голосовое)
            text = f"[голосовое сообщение]: {transcript}"
            # Early persist (DM). В группах лог выше.
            if not is_group:
                try:
                    memory.log_message(user_dir, "user", text)
                except Exception as e:
                    logger.error(f"early_persist voice failed: {e}")
            self._add_to_buffer(chat_id, text, None, context, user_id=update.effective_user.id if update.effective_user else None)

        except ValueError as e:
            logger.error(f"Voice config error: {e}")
            await update.message.reply_text(str(e))
        except Exception as e:
            logger.error(f"Voice processing error: {e}")
            await update.message.reply_text(
                "Не удалось обработать голосовое сообщение."
            )

    # ── Group onboarding ──

    async def _handle_my_chat_member(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Обработка изменения статуса бота в чате (добавлен/удалён)."""
        member_update = update.my_chat_member
        if not member_update:
            return

        chat = member_update.chat
        new_status = member_update.new_chat_member.status
        old_status = member_update.old_chat_member.status

        # Бот добавлен в группу
        if chat.type in ("group", "supergroup"):
            if new_status in ("member", "administrator") and old_status in (
                "left", "kicked", "banned",
            ):
                await self._on_bot_added_to_group(chat, context)
