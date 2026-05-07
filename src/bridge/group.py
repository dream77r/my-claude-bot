"""
Group chat handlers mixin.

Логика, специфичная для групп/супергрупп:
- определение типа чата и упоминания бота
- проверка allow-listed топиков
- onboarding при добавлении в группу (приветствие + DM владельцу)

Mixin предполагает наличие на self: `agent`, _is_group_chat не нужен
больше нигде вне групп. Вызывается из _handle_text/_handle_my_chat_member.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from .. import memory

if TYPE_CHECKING:
    from ..telegram_bridge import TelegramBridge  # noqa: F401

logger = logging.getLogger(__name__)


class GroupChatMixin:
    """
    Группы и форумные топики: детект, фильтр упоминаний, onboarding.
    """

    def _is_group_chat(self: TelegramBridge, update: Update) -> bool:
        """Проверить, является ли чат групповым."""
        chat_type = update.effective_chat.type if update.effective_chat else ""
        return chat_type in ("group", "supergroup")

    def _is_bot_mentioned(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        """Проверить, упомянут ли бот в сообщении (@ или reply)."""
        msg = update.message
        if not msg:
            return False

        # Reply на сообщение бота
        if msg.reply_to_message and msg.reply_to_message.from_user:
            if msg.reply_to_message.from_user.id == context.bot.id:
                return True

        # @username упоминание
        bot_username = context.bot.username
        if bot_username and msg.text:
            if f"@{bot_username}" in msg.text:
                return True

        # Упоминание через entities
        if msg.entities:
            for entity in msg.entities:
                if entity.type == "mention" and msg.text:
                    mentioned = msg.text[entity.offset:entity.offset + entity.length]
                    if bot_username and mentioned == f"@{bot_username}":
                        return True

        return False

    def _is_topic_allowed(
        self: TelegramBridge, chat_id: int, thread_id: int | None
    ) -> bool:
        """Проверить, разрешён ли топик для ответа. True = можно отвечать."""
        allowed = memory.get_group_setting(
            self.agent.agent_dir, chat_id, "allowed_topic"
        )
        if allowed is None:
            return True  # Нет ограничения — отвечаем везде
        if thread_id is None:
            return False  # Есть ограничение, но сообщение не в топике
        return thread_id == allowed

    async def _on_bot_added_to_group(self: TelegramBridge, chat, context) -> None:
        """Бот добавлен в группу — приветствие + DM владельцу."""
        chat_id = chat.id
        chat_title = chat.title or "Без названия"
        chat_type = chat.type or "group"
        is_forum = getattr(chat, "is_forum", False) or False

        # Создать context.md
        memory.create_group_context(
            self.agent.agent_dir, chat_id, chat_title, chat_type
        )
        if is_forum:
            memory.set_group_setting(
                self.agent.agent_dir, chat_id, "is_forum", True
            )

        # Приветствие в группе
        bot_username = context.bot.username or self.agent.display_name
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"Привет! Я — {self.agent.display_name}.\n\n"
                    "Буду следить за контекстом беседы. "
                    f"Упомяните @{bot_username} когда нужна помощь."
                ),
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить приветствие в группу {chat_id}: {e}")

        # DM владельцу — спросить как вести себя
        await self._notify_owner_about_group(chat_id, chat_title, context)

    async def _notify_owner_about_group(
        self: TelegramBridge,
        group_chat_id: int,
        chat_title: str,
        context,
    ) -> None:
        """Уведомить владельца в DM о добавлении в группу."""
        if not self.agent.allowed_users:
            return  # Нет владельца — некому писать

        owner_id = self.agent.allowed_users[0]  # В Telegram user_id == DM chat_id

        is_forum = memory.get_group_setting(
            self.agent.agent_dir, group_chat_id, "is_forum"
        )

        buttons = [
            [
                InlineKeyboardButton(
                    "Настроить",
                    callback_data=f"grp_setup:{group_chat_id}",
                ),
                InlineKeyboardButton(
                    "Пропустить",
                    callback_data=f"grp_skip:{group_chat_id}",
                ),
            ]
        ]

        # Для форумов — доп. кнопка ограничения по теме
        if is_forum:
            buttons.append([
                InlineKeyboardButton(
                    "Только одна тема",
                    callback_data=f"grp_topic:{group_chat_id}",
                ),
                InlineKeyboardButton(
                    "Все темы",
                    callback_data=f"grp_alltopics:{group_chat_id}",
                ),
            ])

        keyboard = InlineKeyboardMarkup(buttons)

        forum_hint = ""
        if is_forum:
            forum_hint = (
                "\n\nВижу, что в группе включены темы (топики). "
                "Могу отвечать во всех или только в одной — выбери ниже."
            )

        try:
            await context.bot.send_message(
                chat_id=owner_id,
                text=(
                    f"Меня добавили в группу «{chat_title}».\n\n"
                    "Как мне себя там вести? Нажми «Настроить» и опиши "
                    "правила — например, роль, тон, темы, ограничения."
                    f"{forum_hint}"
                ),
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить DM владельцу {owner_id}: {e}")

    async def _group_onboarding(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Fallback-онбординг при первом сообщении (если ChatMemberUpdated не сработал)."""
        chat = update.effective_chat
        chat_id = chat.id
        chat_title = chat.title or "Без названия"
        chat_type = chat.type or "group"

        # Создать context.md
        memory.create_group_context(
            self.agent.agent_dir, chat_id, chat_title, chat_type
        )

        # Приветствие в группе
        bot_username = context.bot.username or self.agent.display_name
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"Привет! Я — {self.agent.display_name}.\n\n"
                "Буду следить за контекстом беседы. "
                f"Упомяните @{bot_username} когда нужна помощь."
            ),
        )

        # DM владельцу
        await self._notify_owner_about_group(chat_id, chat_title, context)
