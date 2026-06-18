"""
Telegram Bridge — хэндлеры для Telegram-бота.

Фичи:
- Message aggregation (0.6s буфер перед отправкой в Claude)
- MarkdownV2 автоконвертация
- Сплит длинных сообщений "(n/m)"
- Tool hints — статус инструментов в реальном времени
- Stream delta coalescing — защита от rate limit при обновлении статуса
- Command Router с приоритетами (/stop работает всегда)
- Проверка allowed_users
- Git-backed memory (/memory команды)
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import memory
from .bridge.dashboard import DashboardCommandsMixin
from .bridge.fleet import FleetCommandsMixin
from .bridge.group import GroupChatMixin
from .bridge.messages import MessageHandlerMixin
from .command_router import CommandRouter
from .file_handler import clear_outbox, send_file
from .formatter import (
    TG_MESSAGE_LIMIT,
    escape_markdown_v2,
    format_for_telegram,
    markdown_to_html,
    split_message,
)
from .i18n import t
from .rich_message import has_markdown_table, send_rich_message
from .telegram_retry import tg_retry
from .status_message import (
    EDIT_MIN_INTERVAL,
    STREAM_EDIT_INTERVAL,
    TYPING_KEEPALIVE_INTERVAL,
    StatusMessage,
)

if TYPE_CHECKING:
    from .agent import Agent
    from .bus import FleetBus
    from .main import FleetRuntime

logger = logging.getLogger(__name__)

# Буфер для message aggregation (секунды)
MESSAGE_BUFFER_DELAY = 0.6

# Реэкспорт для обратной совместимости с тестами/модулями, которые
# импортируют эти символы из telegram_bridge (исторически они жили здесь).
__all__ = [
    "TG_MESSAGE_LIMIT",
    "EDIT_MIN_INTERVAL",
    "STREAM_EDIT_INTERVAL",
    "TYPING_KEEPALIVE_INTERVAL",
    "escape_markdown_v2",
    "markdown_to_html",
    "format_for_telegram",
    "split_message",
    "StatusMessage",
]

# Команды для меню бота (кнопка "/" в Telegram)
BOT_COMMANDS = [
    BotCommand("help", "Справка по командам"),
    BotCommand("status", "Статус агента"),
    BotCommand("newsession", "Новая сессия (сброс контекста)"),
    BotCommand("stop", "Остановить текущий запрос"),
    BotCommand("memory", "История изменений памяти"),
    BotCommand("restore", "Откатить память"),
    BotCommand("dream", "Запустить Dream-обработку памяти"),
    BotCommand("recall", "Поиск по памяти и графу: /recall <тема>"),
    BotCommand("model", "Сменить модель Claude"),
    BotCommand("stats", "Статистика использования"),
    BotCommand("agents", "Список всех агентов"),
    BotCommand("create_agent", "Создать нового агента"),
    BotCommand("clone_agent", "Клонировать агента"),
    BotCommand("stop_agent", "Остановить агента"),
    BotCommand("start_agent", "Запустить агента"),
    BotCommand("skills", "Список скиллов агента"),
    BotCommand("newskill", "Создать новый скилл"),
    BotCommand("removeskill", "Удалить скилл"),
    BotCommand("poolskills", "Каталог скиллов из пула"),
    BotCommand("installskill", "Установить скилл из пула"),
    BotCommand("refreshpool", "Обновить кэш пула скиллов"),
    BotCommand("restart", "Перезапустить платформу"),
    BotCommand("newkey", "Создать ключ доступа для пользователя"),
    BotCommand("dashboard", "Открыть веб-дэшборд (Mini App)"),
    BotCommand("setup_dashboard", "Настроить дэшборд (мастер)"),
]

# Команды, которые показываются в меню только у master-агента.
# На non-master ботах они скрываются из `/`-меню (хендлеры остаются
# зарегистрированными и вернут вежливый отказ, если юзер введёт руками).
_MASTER_ONLY_COMMAND_NAMES = frozenset({"dashboard", "setup_dashboard"})


def _commands_for(is_master: bool) -> list[BotCommand]:
    if is_master:
        return BOT_COMMANDS
    return [c for c in BOT_COMMANDS if c.command not in _MASTER_ONLY_COMMAND_NAMES]

# Доступные модели Claude
CLAUDE_MODELS = {
    "haiku": "Haiku — быстрая, дешёвая",
    "sonnet": "Sonnet — баланс скорости и качества",
    "opus": "Opus — максимальное качество",
}


def _main_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура с основными командами."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Статус", callback_data="cmd:status"),
            InlineKeyboardButton("🧠 Память", callback_data="cmd:memory"),
        ],
        [
            InlineKeyboardButton("🔄 Новая сессия", callback_data="cmd:newsession"),
            InlineKeyboardButton("⏹ Стоп", callback_data="cmd:stop"),
        ],
        [
            InlineKeyboardButton("⏪ Откатить память", callback_data="cmd:restore"),
            InlineKeyboardButton("🤖 Модель", callback_data="cmd:model"),
        ],
        [
            InlineKeyboardButton("👥 Агенты", callback_data="cmd:agents"),
            InlineKeyboardButton("🔁 Перезапуск", callback_data="cmd:restart"),
        ],
    ])


def _model_keyboard(current_model: str) -> InlineKeyboardMarkup:
    """Inline-клавиатура для выбора модели."""
    buttons = []
    for model_id, description in CLAUDE_MODELS.items():
        marker = " ✓" if model_id == current_model else ""
        buttons.append([
            InlineKeyboardButton(
                f"{description}{marker}",
                callback_data=f"model:{model_id}",
            )
        ])
    return InlineKeyboardMarkup(buttons)


async def send_long_message(
    chat_id: int,
    text: str,
    context: ContextTypes.DEFAULT_TYPE,
    parse_mode: str | None = None,
    message_thread_id: int | None = None,
    auto_format: bool = True,
) -> None:
    """Отправить сообщение, разбив на части если нужно.

    Если auto_format=True, сначала разбивает raw markdown (не ломая блоки кода),
    затем форматирует каждую часть в HTML отдельно.

    Если текст содержит markdown-таблицы, сначала пробует sendRichMessage (Bot API 10.1)
    с fallback на HTML.
    """
    if auto_format and parse_mode is None:
        # Rich Message path: если есть таблица — пробуем Bot API 10.1
        if has_markdown_table(text):
            success = await send_rich_message(
                context.bot, chat_id, text, message_thread_id
            )
            if success:
                return
            # Fallback: продолжаем с HTML

    if auto_format and parse_mode is None:
        # Сначала режем raw markdown (блоки кода никогда не разрезаются),
        # потом форматируем каждую часть отдельно
        parts = split_message(text)
        for part in parts:
            formatted, pm = format_for_telegram(part)
            try:
                await tg_retry(
                    lambda: context.bot.send_message(
                        chat_id=chat_id,
                        text=formatted,
                        parse_mode=pm,
                        message_thread_id=message_thread_id,
                    ),
                    op="send_long_message",
                )
            except Exception as _e:
                logger.warning(
                    f"send_long_message: HTML parse failed ({_e}), fallback to plain text\n"
                    f"HTML preview: {formatted[:300]!r}"
                )
                await tg_retry(
                    lambda: context.bot.send_message(
                        chat_id=chat_id,
                        text=part,
                        message_thread_id=message_thread_id,
                    ),
                    op="send_long_message.fallback",
                )
            if len(parts) > 1:
                await asyncio.sleep(0.3)
        return

    parts = split_message(text)
    for part in parts:
        try:
            await tg_retry(
                lambda: context.bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    parse_mode=parse_mode,
                    message_thread_id=message_thread_id,
                ),
                op="send_long_message",
            )
        except Exception as _e:
            if parse_mode:
                logger.warning(
                    f"send_long_message: HTML parse failed ({_e}), fallback to plain text\n"
                    f"HTML preview: {part[:300]!r}"
                )
                await tg_retry(
                    lambda: context.bot.send_message(
                        chat_id=chat_id,
                        text=part,
                        message_thread_id=message_thread_id,
                    ),
                    op="send_long_message.fallback",
                )
        if len(parts) > 1:
            await asyncio.sleep(0.3)


class TelegramBridge(
    FleetCommandsMixin,
    DashboardCommandsMixin,
    GroupChatMixin,
    MessageHandlerMixin,
):
    """Мост между Telegram и Agent."""

    def __init__(
        self,
        agent: "Agent",
        semaphore: asyncio.Semaphore,
        bus: "FleetBus | None" = None,
        agent_worker: "AgentWorker | None" = None,
        fleet_runtime: "FleetRuntime | None" = None,
    ):
        from .agent_worker import AgentWorker  # noqa: F811

        self.agent = agent
        self.semaphore = semaphore
        self.bus = bus
        self.agent_worker = agent_worker
        self.fleet_runtime = fleet_runtime

        # Буфер для message aggregation: chat_id → (messages, files, Task)
        self._buffers: dict[int, tuple[list[str], list[str], asyncio.Task]] = {}

        # Активные задачи: chat_id → Task (для /stop без bus)
        self._active_tasks: dict[int, asyncio.Task] = {}

        # Активные StatusMessage per chat (для bus listener)
        self._status_messages: dict[int, StatusMessage] = {}

        # Telegram app (сохраняем для bus listener)
        self._app: Application | None = None

        # Thread ID (топики) для каждого chat_id — для ответа в правильном топике
        self._thread_ids: dict[int, int | None] = {}

        # User ID для каждого chat_id — для per-user директорий в _flush_buffer
        self._user_ids: dict[int, int | None] = {}

        # Pending group setup: owner_dm_chat_id → group_chat_id
        # Когда владелец нажал "Настроить" — ждём текст с правилами
        self._pending_group_setups: dict[int, int] = {}

        # Pending topic setup: group_chat_id → owner_user_id
        # Когда владелец нажал "Только одна тема" — ждём mention в нужном топике
        self._pending_topic_setups: dict[int, int] = {}

        # Состояние визарда создания агента: chat_id → {step, data}
        self._wizard_state: dict[int, dict] = {}

        # Skill Pool — ленивая инициализация (создаётся при первом обращении)
        self._skill_pool_cached = False
        self._skill_pool = None

        # Command router
        self.router = self._build_router()

    def _get_skill_pool(self):
        """
        Получить SkillPool из .env (ленивая инициализация).

        Returns:
            SkillPool или None если SKILL_POOL_URL не задан
        """
        if not self._skill_pool_cached:
            from .skill_pool import make_pool_from_env
            project_root = Path(self.agent.agent_dir).parent.parent
            self._skill_pool = make_pool_from_env(project_root)
            self._skill_pool_cached = True
        return self._skill_pool

    def _get_master_agent_dir(self) -> str | None:
        """Получить agent_dir master-агента (для каскадных настроек)."""
        if not self.fleet_runtime:
            return None
        for agent in self.fleet_runtime.agents.values():
            if agent.is_master:
                return agent.agent_dir
        return None

    def _build_router(self) -> CommandRouter:
        """Создать и настроить роутер команд."""
        router = CommandRouter()

        # Priority — работают даже когда агент занят
        router.priority("/stop", self._cmd_stop)
        router.priority("/restart", self._cmd_restart)

        # Exact — обычные команды
        router.exact("/start", self._cmd_start)
        router.exact("/help", self._cmd_help)
        router.exact("/newsession", self._cmd_newsession)
        router.exact("/memory", self._cmd_memory_log)
        router.exact("/restore", self._cmd_memory_restore)
        router.exact("/status", self._cmd_status)
        router.exact("/dream", self._cmd_dream)
        router.exact("/recall", self._cmd_recall)
        router.exact("/model", self._cmd_model)
        router.exact("/stats", self._cmd_stats)

        # Agent Manager commands
        router.exact("/agents", self._cmd_agents)
        router.exact("/create_agent", self._cmd_create_agent)
        router.exact("/clone_agent", self._cmd_clone_agent)
        router.exact("/set_access", self._cmd_set_access)
        router.exact("/stop_agent", self._cmd_stop_agent)
        router.exact("/start_agent", self._cmd_start_agent)

        # Skill Creator commands
        router.exact("/skills", self._cmd_skills)
        router.exact("/newskill", self._cmd_newskill)
        router.exact("/removeskill", self._cmd_removeskill)

        # Skill Pool commands (маркетплейс скиллов)
        router.exact("/poolskills", self._cmd_poolskills)
        router.exact("/installskill", self._cmd_installskill)
        router.exact("/refreshpool", self._cmd_refreshpool)

        # Mini App dashboard
        router.exact("/dashboard", self._cmd_dashboard)
        router.exact("/setup_dashboard", self._cmd_setup_dashboard)

        # Multi-user access key commands
        router.exact("/newkey", self._cmd_newkey)

        # GitHub Backup commands
        router.exact("/backup_link", self._cmd_backup_link)
        router.exact("/backup_now", self._cmd_backup_now)
        router.exact("/backup_status", self._cmd_backup_status)

        return router

    def build_app(self) -> Application:
        """Создать и настроить Telegram Application."""
        app = Application.builder().token(self.agent.bot_token).build()

        # Бот добавлен/удалён из чата
        app.add_handler(ChatMemberHandler(
            self._handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER
        ))

        # Callback-кнопки (inline keyboard)
        app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Единый хэндлер для всех команд — через роутер
        app.add_handler(MessageHandler(
            filters.COMMAND, self._handle_command
        ))

        # Сообщения
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self._handle_text
        ))
        app.add_handler(MessageHandler(
            filters.Document.ALL, self._handle_document
        ))
        app.add_handler(MessageHandler(
            filters.PHOTO, self._handle_photo
        ))
        app.add_handler(MessageHandler(
            filters.VOICE | filters.AUDIO, self._handle_voice
        ))

        # Зарегистрировать меню команд + git при старте
        app.post_init = self._post_init

        return app

    async def _post_init(self, app: Application) -> None:
        """Выполняется после инициализации бота: меню + git."""
        # Зарегистрировать кнопку-меню "/" в Telegram
        try:
            await app.bot.set_my_commands(_commands_for(self.agent.is_master))
            logger.info(f"Bot menu commands registered for '{self.agent.name}'")
        except Exception as e:
            logger.warning(f"Failed to set bot commands: {e}")

        # Инициализировать git в memory/
        memory.git_init(self.agent.agent_dir)

    def _check_auth(self, update: Update) -> bool:
        """Проверить авторизацию пользователя.

        В группах: при @mention разрешаем всем участникам.
        В DM: только зарегистрированные пользователи (multi_user) или allowed_users.
        """
        user = update.effective_user
        if not user:
            return False
        if self._is_group_chat(update):
            return True  # В группах auth по-другому — отвечаем любому при mention
        if self.agent.is_multi_user:
            return self.agent.is_user_registered(user.id)
        return self.agent.is_user_allowed(user.id)

    def _get_sender_name(self, update: Update) -> str:
        """Получить имя отправителя для логирования."""
        user = update.effective_user
        if not user:
            return "Аноним"
        return user.first_name or user.username or "Аноним"

    def _lang(self) -> str:
        """Получить язык пользователя из settings."""
        return memory.get_setting(self.agent.agent_dir, "language") or "ru"

    def _effective_dir(self, update: Update) -> str:
        """Return per-user directory in multi_user mode, otherwise agent_dir."""
        if self.agent.is_multi_user and update.effective_user:
            return self.agent.get_effective_dir(update.effective_user.id)
        return self.agent.agent_dir

    # Команды, доступные только владельцу (allowed_users) в группах
    _OWNER_ONLY_COMMANDS = {
        "/model", "/restore", "/dream", "/newsession", "/memory", "/start",
        "/agents", "/create_agent", "/stop_agent", "/start_agent", "/restart",
        "/setup_dashboard",
    }

    # ── Unified command handler ──

    async def _handle_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Единый хэндлер для всех команд через CommandRouter."""
        text = update.message.text
        is_group = self._is_group_chat(update)

        if is_group:
            # В группах: игнорировать команды без @botname (могут быть для другого бота)
            bot_username = context.bot.username
            if bot_username and "@" in text.split()[0]:
                # Команда адресована конкретному боту — проверить что нашему
                if f"@{bot_username}" not in text.split()[0]:
                    return
            elif bot_username and "@" not in text.split()[0]:
                # Команда без @botname — в группе с ботами может быть не для нас
                # Обрабатываем только если бот единственный (оставляем для обратной совместимости)
                pass

            # Проверка прав в группе
            cmd = text.split()[0].split("@")[0].lower()
            user = update.effective_user
            if cmd in self._OWNER_ONLY_COMMANDS:
                if not user or not self.agent.is_user_allowed(user.id):
                    await update.message.reply_text("Эта команда доступна только владельцу.")
                    return
        else:
            # В DM: /start пропускает auth (для регистрации нового клиента)
            cmd = text.split()[0].split("@")[0].lower()
            if cmd != "/start" and not self._check_auth(update):
                if self.agent.is_multi_user:
                    await update.message.reply_text(
                        "Доступ к этому боту — по приглашению. "
                        "Запросите ключ у владельца и используйте команду /start <ключ>."
                    )
                return

        result = self.router.route(text)

        if result:
            await result.handler(update, context, result.args)
        elif not is_group:
            await update.message.reply_text(
                "Неизвестная команда. /help для списка."
            )

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Обработчик нажатий на inline-кнопки."""
        query = update.callback_query
        if not query or not query.data:
            return

        # Проверить авторизацию
        user = query.from_user
        if not user:
            await query.answer("Нет доступа", show_alert=True)
            return
        if self.agent.is_multi_user:
            if not self.agent.is_user_registered(user.id):
                await query.answer("Нет доступа", show_alert=True)
                return
        elif not self.agent.is_user_allowed(user.id):
            await query.answer("Нет доступа", show_alert=True)
            return

        # Убрать "часики" на кнопке
        await query.answer()

        # Определить директорию пользователя
        user_dir = self.agent.get_effective_dir(user.id)

        # Выбор языка: lang:en, lang:ru
        if query.data.startswith("lang:"):
            lang = query.data[5:]
            memory.set_setting(user_dir, "language", lang)
            try:
                confirm = {"en": "English selected! Let's get started.", "ru": "Отлично! Давай начнём."}
                await query.edit_message_text(confirm.get(lang, confirm["en"]))
            except Exception:
                pass
            # Запустить онбординг после выбора языка
            await self._start_onboarding(update, context)
            return

        # Выбор модели: model:sonnet, model:opus, model:haiku
        if query.data.startswith("model:"):
            model_id = query.data[6:]
            if model_id in CLAUDE_MODELS:
                memory.set_setting(user_dir, "claude_model", model_id)
                # Обновить кнопки — показать галочку на выбранной модели
                try:
                    await query.edit_message_text(
                        f"Модель изменена на {CLAUDE_MODELS[model_id]}",
                        reply_markup=_model_keyboard(model_id),
                    )
                except Exception:
                    pass
            return

        # Настройка группы из DM: grp_setup:{chat_id}
        if query.data.startswith("grp_setup:"):
            group_chat_id = int(query.data[10:])
            owner_chat_id = query.from_user.id
            self._pending_group_setups[owner_chat_id] = group_chat_id
            try:
                await query.edit_message_text(
                    "Опиши, как мне вести себя в этой группе.\n\n"
                    "Например: роль, тон общения, темы, ограничения. "
                    "Просто напиши текстом в следующем сообщении."
                )
            except Exception:
                pass
            return

        # Ограничить бота одной темой: grp_topic:{chat_id}
        if query.data.startswith("grp_topic:"):
            group_chat_id = int(query.data[10:])
            owner_id = query.from_user.id
            self._pending_topic_setups[group_chat_id] = owner_id
            try:
                await query.edit_message_text(
                    "Упомяни меня (@) в нужной теме группы.\n"
                    "Я запомню её как единственную для ответов."
                )
            except Exception:
                pass
            return

        # Разрешить все темы: grp_alltopics:{chat_id}
        if query.data.startswith("grp_alltopics:"):
            group_chat_id = int(query.data[14:])
            memory.set_group_setting(
                self.agent.agent_dir, group_chat_id, "allowed_topic", None
            )
            try:
                await query.edit_message_text(
                    "Буду отвечать во всех темах группы."
                )
            except Exception:
                pass
            return

        # Пропустить настройку группы: grp_skip:{chat_id}
        if query.data.startswith("grp_skip:"):
            try:
                await query.edit_message_text(
                    "Ок, буду вести себя по умолчанию. "
                    "Настроить можно позже — отправь мне правила и "
                    "укажи для какой группы."
                )
            except Exception:
                pass
            return

        # Маппинг callback_data → команда роутера
        if query.data.startswith("cmd:"):
            cmd = "/" + query.data[4:]
            result = self.router.route(cmd)
            if result:
                await result.handler(update, context, result.args)

    # ── Команды ──

    async def _cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        user = update.effective_user

        # ── Multi-user mode: key-based registration ──
        if self.agent.is_multi_user and user:
            if self.agent.is_user_registered(user.id):
                # Already registered — normal welcome
                user_dir = self.agent.get_effective_dir(user.id)
                if memory.is_onboarding_needed(user_dir):
                    if not memory.get_setting(user_dir, "language"):
                        await self._ask_language(update, context)
                    else:
                        await self._start_onboarding(update, context)
                else:
                    lang = self._lang()
                    await update.message.reply_text(
                        t("start_greeting", lang, display_name=self.agent.display_name),
                        reply_markup=_main_keyboard(),
                    )
                return

            key = args.strip() if args else ""
            if not key:
                await update.message.reply_text(
                    "Доступ к этому боту — по приглашению.\n"
                    "Запросите ключ у владельца и откройте ссылку из приглашения."
                )
                return

            valid, reason = self.agent.validate_key(key)
            if not valid:
                if reason == "used":
                    await update.message.reply_text(
                        "Этот ключ уже использован другим пользователем."
                    )
                else:
                    await update.message.reply_text(
                        "Ключ не найден или недействителен. Запросите новый у владельца."
                    )
                return

            # Activate key and create user directory
            self.agent.activate_key(key, user.id)
            user_dir = self.agent.get_effective_dir(user.id)
            logger.info(f"New user {user.id} ({user.first_name}) registered via key")

            if not memory.get_setting(user_dir, "language"):
                await self._ask_language(update, context)
            else:
                await self._start_onboarding(update, context)
            return

        # ── Single-user mode: авто-регистрация первого клиента ──
        if user and self.agent.allowed_users:
            if user.id not in self.agent.allowed_users:
                import os
                founder_id = int(os.environ.get("FOUNDER_TELEGRAM_ID", "0") or "0")
                clients = [uid for uid in self.agent.allowed_users if uid != founder_id]
                if clients:
                    await update.message.reply_text(
                        "Этот бот уже привязан к другому пользователю."
                    )
                    return
                self.agent.allowed_users.append(user.id)
                self._save_allowed_users(user.id, user.first_name or "")

        # Проверить нужен ли онбординг (проверяем profile.md каждый раз)
        if memory.is_onboarding_needed(self.agent.agent_dir):
            if not memory.get_setting(self.agent.agent_dir, "language"):
                await self._ask_language(update, context)
            else:
                await self._start_onboarding(update, context)
        else:
            lang = self._lang()
            chat_id = update.effective_chat.id if update.effective_chat else None
            send = update.message.reply_text if update.message else (
                lambda **kw: context.bot.send_message(chat_id=chat_id, **kw)
            )
            await send(
                text=t("start_greeting", lang, display_name=self.agent.display_name),
                reply_markup=_main_keyboard(),
            )

    async def _cmd_newkey(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Generate a new single-use access key (owner only)."""
        if not self.agent.is_multi_user:
            await self._reply(
                update, context,
                "Мультипользовательский режим отключён. "
                "Добавьте multi_user: true в agent.yaml чтобы использовать ключи доступа."
            )
            return

        user = update.effective_user
        if not user or not self.agent.is_user_allowed(user.id):
            await self._reply(update, context, "Только владелец может создавать ключи доступа.")
            return

        key = self.agent.generate_key()
        bot_info = await context.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={key}"
        await self._reply(
            update, context,
            f"Ключ создан. Отправьте эту ссылку пользователю:\n\n{link}\n\n"
            "Ключ одноразовый — после использования сгорает."
        )

    async def _ask_language(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Показать кнопки выбора языка перед онбордингом."""
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
                InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
            ]
        ])
        chat_id = update.effective_chat.id
        # Показать на обоих языках
        await context.bot.send_message(
            chat_id=chat_id,
            text="Choose your language / Выбери язык общения:",
            reply_markup=keyboard,
        )

    async def _start_onboarding(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Запустить процесс знакомства — отправить первое сообщение в Claude."""
        chat_id = update.effective_chat.id
        lang = self._lang()

        onboarding_prompt = t("onboarding_prompt", lang)

        # Status message вместо голого typing
        status = StatusMessage(chat_id, context)
        await status.show(t("starting", lang))

        try:
            user = update.effective_user
            uid = user.id if user else None
            response = await self.agent.call_claude(
                onboarding_prompt,
                None,
                self.semaphore,
                user_id=uid,
            )
            memory.log_message(self._effective_dir(update), "assistant", response)
            await status.cleanup()
            await send_long_message(chat_id, response, context)
        except Exception as e:
            logger.error(f"Onboarding error: {e}")
            await status.cleanup()
            await context.bot.send_message(
                chat_id=chat_id,
                text=t("onboarding_fallback", lang, display_name=self.agent.display_name),
            )

    async def _cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        chat_id = update.effective_chat.id if update.effective_chat else None
        lang = self._lang()
        text = t("help_text", lang)

        if update.message:
            await update.message.reply_text(text, reply_markup=_main_keyboard())
        elif chat_id:
            await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=_main_keyboard()
            )

    async def _reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Универсальный ответ: работает и из команды, и из callback-кнопки."""
        chat_id = update.effective_chat.id
        if update.message:
            await tg_retry(
                lambda: update.message.reply_text(
                    text, reply_markup=reply_markup
                ),
                op="reply.message",
            )
        else:
            await tg_retry(
                lambda: context.bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup
                ),
                op="reply.callback",
            )

    async def _cmd_newsession(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        memory.clear_session_id(self._effective_dir(update))
        await self._reply(
            update, context,
            "Новая сессия начата. Контекст предыдущей сессии сброшен."
        )

    async def _cmd_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Остановить текущий запрос. Priority-команда — работает всегда."""
        chat_id = update.effective_chat.id
        stopped = False

        # Попробовать через agent_worker (bus-режим)
        if self.agent_worker:
            stopped = self.agent_worker.cancel_task(chat_id)

        # Fallback: через _active_tasks (прямой режим)
        if not stopped:
            task = self._active_tasks.get(chat_id)
            if task and not task.done():
                task.cancel()
                self._active_tasks.pop(chat_id, None)
                stopped = True

        # Очистить статус если был
        status = self._status_messages.pop(chat_id, None)
        if status:
            await status.cleanup()

        if stopped:
            await self._reply(update, context, "Остановлено.")
        else:
            await self._reply(update, context, "Нет активного запроса.")

    async def _cmd_restart(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Перезапустить платформу. Systemd поднимет процесс заново."""
        import signal

        await self._reply(
            update, context,
            "Перезапускаюсь... Буду доступен через 5-10 секунд."
        )
        await asyncio.sleep(1)
        logger.info("Restart requested via Telegram")
        # SIGINT → asyncio.run переведёт в KeyboardInterrupt → graceful shutdown
        # в async_main (flush git_committer, закрытие bridges). systemd Restart=always
        # поднимет процесс заново. Fallback: если shutdown завис (напр. httpx pool
        # timeout), через 15с добиваем os._exit, чтобы systemd гарантированно
        # увидел завершение. sys.exit тут не работает — SystemExit проглатывается
        # внутри handler-таски PTB и основной loop продолжает крутиться.
        asyncio.get_running_loop().call_later(15.0, os._exit, 0)
        os.kill(os.getpid(), signal.SIGINT)

    async def _cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Показать статус агента."""
        chat_id = update.effective_chat.id
        is_busy = chat_id in self._active_tasks and not self._active_tasks[chat_id].done()
        user_dir = self._effective_dir(update)
        session = memory.get_session_id(user_dir)

        status_lines = [
            f"Агент: {self.agent.display_name}",
            f"Статус: {'обрабатываю запрос' if is_busy else 'свободен'}",
            f"Сессия: {'активна' if session else 'новая'}",
        ]

        # Git info
        log_entries = memory.git_log(user_dir, limit=1)
        if log_entries:
            last = log_entries[0]
            status_lines.append(f"Последний бэкап памяти: {last['date']}")

        await self._reply(update, context, "\n".join(status_lines))

    async def _cmd_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Показать статистику использования."""
        from .metrics import format_stats, get_stats

        # Парсить период: /stats 7 → за 7 дней
        days = 1
        if args.strip().isdigit():
            days = int(args.strip())
            days = max(1, min(days, 90))  # Лимит 1-90 дней

        stats = get_stats(self._effective_dir(update), days=days)
        text = format_stats(stats)
        await self._reply(update, context, text)

    async def _cmd_memory_log(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Показать историю изменений памяти."""
        entries = memory.git_log(self._effective_dir(update), limit=10)

        if not entries:
            await self._reply(
                update, context,
                "История памяти пуста. Память будет версионироваться автоматически."
            )
            return

        lines = ["История изменений памяти:\n"]
        for entry in entries:
            lines.append(f"  {entry['hash']} | {entry['date']} | {entry['message']}")

        await self._reply(update, context, "\n".join(lines))

    async def _cmd_memory_restore(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Откатить память к предыдущей версии."""
        commit_hash = args.strip() if args.strip() else None

        if memory.git_restore(self._effective_dir(update), commit_hash):
            target = commit_hash or "предыдущая версия"
            await self._reply(
                update, context,
                f"Память откачена к: {target}\n"
                "Используй /memory чтобы посмотреть историю."
            )
        else:
            await self._reply(
                update, context,
                "Не удалось откатить. Проверь /memory для списка версий."
            )

    async def _cmd_dream(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Запустить Dream-цикл вручную."""
        from .dream import dream_cycle

        chat_id = update.effective_chat.id
        status = StatusMessage(chat_id, context)
        await status.show("Запускаю Dream-обработку памяти...")

        try:
            result = await dream_cycle(self._effective_dir(update))
            await status.cleanup()

            lines = ["Dream-цикл завершён:"]
            lines.append(f"  Фактов извлечено: {result['facts_count']}")
            if result["summary"]:
                lines.append(f"  Резюме: {result['summary']}")
            lines.append(
                f"  Phase 1: {'ok' if result['phase1_ok'] else 'пропущена'}"
            )
            lines.append(
                f"  Phase 2: {'ok' if result['phase2_ok'] else 'пропущена'}"
            )
            await self._reply(update, context, "\n".join(lines))
        except Exception as e:
            await status.cleanup()
            logger.error(f"Dream command error: {e}")
            await self._reply(
                update, context,
                f"Ошибка Dream-цикла: {e}"
            )

    async def _cmd_recall(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """
        /recall <тема> — поиск по локальной wiki/ и графу.

        Просто проксирует запрос мастер-агенту с явной инструкцией
        использовать навык wiki-search. Сам поиск (BM25 + BFS + цитаты)
        выполняется детерминированным CLI src.wiki_search изнутри навыка.
        """
        query = (args or "").strip()
        if not query:
            await self._reply(
                update, context,
                "Использование: /recall <тема>\n\n"
                "Например: /recall Phase 5",
            )
            return

        chat_id = update.effective_chat.id
        prompt = (
            f"Используй навык wiki-search для поиска по памяти. "
            f"Тема: {query}\n\n"
            f"Запусти CLI `python3 -m src.wiki_search --agent agents/me "
            f"--query \"{query}\"` из корня проекта, затем сформулируй "
            f"человеческий ответ по данным навыка. Если в памяти ничего "
            f"не найдено — честно скажи об этом, не выдумывай."
        )
        user_id = update.effective_user.id if update.effective_user else None
        # Early persist (только если это не группа).
        if not self._is_group_chat(update):
            try:
                memory.log_message(
                    self._effective_dir(update), "user", f"/recall {query}"
                )
            except Exception as e:
                logger.error(f"early_persist recall failed: {e}")
        self._add_to_buffer(chat_id, prompt, None, context, user_id=user_id)

    async def _cmd_model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Показать / сменить модель Claude."""
        # Текущая модель: settings override → agent.yaml default
        current = (
            memory.get_setting(self._effective_dir(update), "claude_model")
            or self.agent.claude_model
        )

        chat_id = update.effective_chat.id
        text = f"Текущая модель: {CLAUDE_MODELS.get(current, current)}\n\nВыбери модель:"

        if update.message:
            await update.message.reply_text(
                text, reply_markup=_model_keyboard(current)
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=_model_keyboard(current)
            )

    # ── Fleet & Skill commands → bridge/fleet.py ──


    # ── Mini App dashboard + GitHub backup commands → bridge/dashboard.py ──

    # ── Message aggregation ──

    # ── Message handlers + buffer + my_chat_member → bridge/messages.py ──

    async def start_bus_listener(self, app: Application) -> None:
        """
        Слушать outbound-сообщения из bus и отправлять в Telegram.

        Вызывается после инициализации app.
        """
        if not self.bus:
            return

        queue_name = f"telegram:{self.agent.name}"
        self.bus.subscribe(queue_name)

        logger.info(f"Bus listener запущен для '{queue_name}'")

        while True:
            try:
                msg = await self.bus.consume(queue_name)
                chat_id = msg.chat_id
                if not chat_id:
                    # Fallback: попытаться получить FOUNDER_TELEGRAM_ID
                    # (используется для cron/reminder-уведомлений от worker-агентов)
                    try:
                        founder_id = int(os.environ.get("FOUNDER_TELEGRAM_ID", "0") or "0")
                        if founder_id:
                            chat_id = founder_id
                            logger.debug(
                                f"Bus message: используем fallback chat_id={founder_id} "
                                f"(FOUNDER_TELEGRAM_ID)"
                            )
                        else:
                            # Ни FOUNDER_TELEGRAM_ID не установлен, ни сообщение не имеет chat_id
                            logger.warning(
                                f"Bus message: chat_id=0 и FOUNDER_TELEGRAM_ID не установлен, "
                                f"сообщение пропущено. "
                                f"Установите FOUNDER_TELEGRAM_ID=<ваш_telegram_id> в .env"
                            )
                            continue
                    except (ValueError, TypeError):
                        logger.warning(
                            f"Bus message: невалидный FOUNDER_TELEGRAM_ID, "
                            f"сообщение пропущено"
                        )
                        continue

                event = msg.metadata.get("event", "")
                thread_id = msg.metadata.get("message_thread_id")

                if event == "processing_started":
                    # Статус уже создан в _flush_buffer — ничего не делаем
                    pass

                elif event == "tool_use":
                    # Master-агент показывает tool hints, worker — только таймер
                    if self.agent.is_master:
                        status = self._status_messages.get(chat_id)
                        if status:
                            status.stop_thinking_timer()
                            await status.show(f"⏳ {msg.content}")

                elif event == "text_delta":
                    # Streaming: показать накопленный текст (быстрый интервал)
                    status = self._status_messages.get(chat_id)
                    if status:
                        status.stop_thinking_timer()
                        # Обрезать до лимита Telegram
                        preview = msg.content[:TG_MESSAGE_LIMIT - 20]
                        if len(msg.content) > TG_MESSAGE_LIMIT - 20:
                            preview += "\n..."
                        await status.show(preview, streaming=True)

                elif event == "response":
                    # Финальный ответ
                    status = self._status_messages.pop(chat_id, None)
                    self._log_assistant_reply(chat_id, msg.content, app)
                    # Попробовать edit на месте (без flash)
                    finalized = False
                    if status and not msg.files:
                        finalized = await status.finalize(msg.content)
                    elif status:
                        await status.cleanup()
                    # Если edit не удался — отправить новым сообщением
                    if not finalized:
                        await self._send_via_bot(
                            app, chat_id, msg.content, thread_id
                        )
                    # Отправить файлы из outbox (если есть)
                    if msg.files:
                        await self._send_outbox_files(
                            app, chat_id, msg.files, thread_id
                        )
                        # Очистить outbox после успешной отправки
                        agent_dir = msg.metadata.get("agent_dir")
                        if agent_dir:
                            clear_outbox(agent_dir)

                elif event == "interrupted":
                    # Turn был отменён извне (например /stop). Финализируем
                    # статус с пометкой, что ответ прерван.
                    status = self._status_messages.pop(chat_id, None)
                    if status:
                        partial = status.current_text() or ""
                        if partial.strip():
                            await status.finalize(f"{partial}\n\n_(прервано)_")
                        else:
                            await status.cleanup()

                elif event == "queued_followup":
                    # Юзер прислал сообщение во время активного turn —
                    # оно буферизовано и отработает следующим turn'ом.
                    # Статус-сообщение уже видно, явного ack не шлём,
                    # чтобы не засорять чат.
                    pass

                elif event == "error":
                    status = self._status_messages.pop(chat_id, None)
                    if status:
                        await status.cleanup()
                    await self._send_via_bot(
                        app, chat_id, msg.content, thread_id
                    )
                    self._log_assistant_reply(chat_id, msg.content, app)

                elif msg.msg_type.value == "outbound" and not event:
                    # Generic outbound (cron/heartbeat/dispatcher notifications).
                    # thread_id читаем из metadata, чтобы сообщение попало
                    # в нужный топик, а не в главный тред группы.
                    await self._send_via_bot(
                        app, chat_id, msg.content, thread_id
                    )
                    self._log_assistant_reply(chat_id, msg.content, app)

            except asyncio.CancelledError:
                logger.info(f"Bus listener '{queue_name}' остановлен")
                break
            except Exception as e:
                logger.error(f"Bus listener error: {e}")

    def _log_assistant_reply(
        self, chat_id: int, text: str, app: Application
    ) -> None:
        """Единая точка записи ответа ассистента.

        DM (chat_id >= 0) → memory/daily/ (персональный лог).
        Группа (chat_id < 0) → memory/groups/{chat_id}/daily/.
        Один ответ = один лог-файл, без дублей.
        """
        try:
            if chat_id >= 0:
                memory.log_message(self.agent.agent_dir, "assistant", text)
            else:
                bot_name = (
                    getattr(app.bot, "first_name", None)
                    or getattr(app.bot, "username", None)
                    or self.agent.name
                )
                memory.log_group_message(
                    self.agent.agent_dir,
                    chat_id,
                    bot_name,
                    text,
                    role="assistant",
                )
        except Exception as e:
            logger.error(f"_log_assistant_reply error: {e}")

    async def _send_via_bot(
        self,
        app: Application,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
    ) -> None:
        """Отправить сообщение через бота (из bus listener)."""
        # Сначала делим raw markdown (блоки кода не разрезаются),
        # потом форматируем каждую часть отдельно — чтобы <pre><code>
        # попадал в send_message целым и Telegram показывал кнопку Copy.
        parts = split_message(text)
        for part in parts:
            formatted_part, parse_mode = format_for_telegram(part)
            try:
                await tg_retry(
                    lambda: app.bot.send_message(
                        chat_id=chat_id,
                        text=formatted_part,
                        parse_mode=parse_mode,
                        message_thread_id=message_thread_id,
                    ),
                    op="send_via_bot",
                )
            except Exception as e:
                if parse_mode:
                    logger.warning(
                        f"HTML send failed for chat {chat_id}: {e}\n"
                        f"HTML preview: {formatted_part[:300]!r}"
                    )
                    try:
                        await tg_retry(
                            lambda: app.bot.send_message(
                                chat_id=chat_id,
                                text=part,
                                message_thread_id=message_thread_id,
                            ),
                            op="send_via_bot.fallback",
                        )
                    except Exception as e2:
                        logger.error(f"Send error to {chat_id}: {e2}")
                else:
                    logger.error(f"Send error to {chat_id}: {e}")
            if len(parts) > 1:
                await asyncio.sleep(0.3)

    async def _send_outbox_files(
        self,
        app: Application,
        chat_id: int,
        file_paths: list[str],
        message_thread_id: int | None = None,
    ) -> None:
        """Отправить файлы из outbox в Telegram чат."""
        for fpath in file_paths:
            try:
                await send_file(app.bot, chat_id, fpath, message_thread_id=message_thread_id)
                logger.info(f"Outbox файл отправлен: {fpath} → {chat_id} thread {message_thread_id}")
            except Exception as e:
                logger.error(f"Outbox send error ({fpath}): {e}")
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=f"Не удалось отправить файл: {Path(fpath).name}",
                        message_thread_id=message_thread_id,
                    )
                except Exception:
                    pass
