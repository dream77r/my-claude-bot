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
    MenuButtonWebApp,
    Update,
    WebAppInfo,
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
from .command_router import CommandRouter
from .file_handler import clear_outbox, download_file, send_file
from .formatter import (
    TG_MESSAGE_LIMIT,
    escape_markdown_v2,
    format_for_telegram,
    markdown_to_html,
    split_message,
)
from .i18n import t
from .status_message import (
    EDIT_MIN_INTERVAL,
    STREAM_EDIT_INTERVAL,
    TYPING_KEEPALIVE_INTERVAL,
    StatusMessage,
)
from .voice_handler import download_voice, get_deepgram_api_key, transcribe

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
    """
    if auto_format and parse_mode is None:
        # Сначала режем raw markdown (блоки кода никогда не разрезаются),
        # потом форматируем каждую часть отдельно
        parts = split_message(text)
        for part in parts:
            formatted, pm = format_for_telegram(part)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=formatted,
                    parse_mode=pm,
                    message_thread_id=message_thread_id,
                )
            except Exception as _e:
                logger.warning(
                    f"send_long_message: HTML parse failed ({_e}), fallback to plain text\n"
                    f"HTML preview: {formatted[:300]!r}"
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    message_thread_id=message_thread_id,
                )
            if len(parts) > 1:
                await asyncio.sleep(0.3)
        return

    parts = split_message(text)
    for part in parts:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=part,
                parse_mode=parse_mode,
                message_thread_id=message_thread_id,
            )
        except Exception as _e:
            if parse_mode:
                logger.warning(
                    f"send_long_message: HTML parse failed ({_e}), fallback to plain text\n"
                    f"HTML preview: {part[:300]!r}"
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    message_thread_id=message_thread_id,
                )
        if len(parts) > 1:
            await asyncio.sleep(0.3)


class TelegramBridge:
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
            await app.bot.set_my_commands(BOT_COMMANDS)
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
            await update.message.reply_text(
                text, reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup
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
        import sys

        await self._reply(
            update, context,
            "Перезапускаюсь... Буду доступен через 5-10 секунд."
        )
        await asyncio.sleep(1)
        logger.info("Restart requested via Telegram")
        # sys.exit(0) → systemd (user) видит что процесс завершился → перезапускает
        sys.exit(0)

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

    # ── Agent Manager commands ──

    async def _cmd_agents(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Список всех агентов и их статус."""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        agents = self.fleet_runtime.manager.list_agents()
        if not agents:
            await self._reply(update, context, "Агенты не найдены.")
            return

        lines = ["Агенты:\n"]
        for a in agents:
            is_running = self.fleet_runtime.is_running(a["name"])
            if is_running:
                status = "🟢 запущен"
            elif a["token_set"]:
                status = "🔴 остановлен"
            else:
                status = "⚪ нет токена"

            lines.append(
                f"  {a['name']} — {a['display_name']}\n"
                f"    Модель: {a['model']} | {status}"
            )

        lines.append(f"\nВсего: {len(agents)}")
        await self._reply(update, context, "\n".join(lines))

    async def _cmd_create_agent(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Начать визард создания агента."""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        chat_id = update.effective_chat.id
        self._wizard_state[chat_id] = {"step": "name", "data": {}}

        await self._reply(
            update, context,
            "Создание нового агента.\n\n"
            "Шаг 1/6: Введи имя агента (латиницей, для папки).\n"
            "Пример: researcher, writer, support\n\n"
            "Отправь /cancel чтобы отменить."
        )

    async def _wizard_handle_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        """Обработать ввод пользователя в режиме визарда."""
        chat_id = update.effective_chat.id
        state = self._wizard_state.get(chat_id)
        if not state:
            return

        # Отмена
        if text.strip().lower() in ("/cancel", "отмена"):
            self._wizard_state.pop(chat_id, None)
            await self._reply(update, context, "Создание агента отменено.")
            return

        # Clone wizard (шаги clone_*)
        if state["step"].startswith("clone_"):
            await self._clone_wizard_handle(update, context, text)
            return

        step = state["step"]
        data = state["data"]

        if step == "name":
            name = text.strip().lower()
            # Валидация
            from .agent_manager import AGENT_NAME_RE
            if not AGENT_NAME_RE.match(name):
                await self._reply(
                    update, context,
                    "Имя должно быть латиницей (a-z, 0-9, -, _), начинаться с буквы.\n"
                    "Попробуй ещё раз:"
                )
                return
            if (self.fleet_runtime.root / "agents" / name).exists():
                await self._reply(
                    update, context,
                    f"Агент '{name}' уже существует. Выбери другое имя:"
                )
                return
            data["name"] = name
            state["step"] = "display_name"
            await self._reply(
                update, context,
                f"Имя: {name}\n\n"
                "Шаг 2/6: Отображаемое имя (на русском).\n"
                "Пример: Исследователь, Копирайтер, Поддержка"
            )

        elif step == "display_name":
            data["display_name"] = text.strip()
            state["step"] = "token"
            await self._reply(
                update, context,
                f"Название: {data['display_name']}\n\n"
                "Шаг 3/6: Токен бота от @BotFather.\n"
                "Создай бота в Telegram через @BotFather и пришли токен."
            )

        elif step == "token":
            token = text.strip()
            from .agent_manager import BOT_TOKEN_RE
            if not BOT_TOKEN_RE.match(token):
                await self._reply(
                    update, context,
                    "Невалидный токен. Формат: цифры:буквы\n"
                    "Получить: @BotFather → /newbot\n"
                    "Попробуй ещё раз:"
                )
                return
            data["token"] = token
            state["step"] = "description"
            await self._reply(
                update, context,
                "Шаг 4/6: Описание роли (одно предложение).\n"
                "Пример: AI-исследователь, помогает находить и анализировать информацию"
            )

        elif step == "description":
            data["description"] = text.strip()
            state["step"] = "model"
            await self._reply(
                update, context,
                f"Роль: {data['description']}\n\n"
                "Шаг 5/6: Модель Claude.\n"
                "Варианты: haiku (быстрая), sonnet (баланс), opus (максимум)\n"
                "Просто напиши название или нажми Enter для sonnet."
            )

        elif step == "model":
            model = text.strip().lower()
            if model not in ("haiku", "sonnet", "opus"):
                model = "sonnet"
            data["model"] = model

            state["step"] = "users"
            await self._reply(
                update, context,
                f"Модель: {model}\n\n"
                "Шаг 6/6: Для кого этот агент?\n\n"
                "Варианты:\n"
                "- Перешли мне сообщение от клиента — я возьму его ID автоматически\n"
                "- Введи Telegram ID вручную (число)\n"
                "- Напиши 'все' — бот будет доступен всем\n"
                "- Напиши 'я' — только для тебя"
            )

        elif step == "users":
            user_ids = []
            input_text = text.strip().lower()

            if input_text in ("я", "me", "i"):
                # Только текущий пользователь (owner)
                user_ids = []  # FOUNDER подставится автоматически
            elif input_text in ("все", "all", "любой", "everyone"):
                user_ids = []  # Пустой список = доступ для всех
                data["open_access"] = True
            else:
                # Попробовать парсить как числа (ID)
                for part in text.replace(",", " ").split():
                    part = part.strip()
                    if part.isdigit():
                        user_ids.append(int(part))

                # Проверить forwarded message
                fwd_origin = getattr(update.message, "forward_origin", None)
                if fwd_origin and getattr(fwd_origin, "type", "") == "user":
                    user_ids.append(fwd_origin.sender_user.id)

            data["allowed_users"] = user_ids

            # Описание доступа
            if data.get("open_access"):
                access_desc = "все (открытый доступ)"
            elif user_ids:
                access_desc = f"ты + {', '.join(str(uid) for uid in user_ids)}"
            else:
                access_desc = "только ты"

            # Показать подтверждение
            state["step"] = "confirm"
            await self._reply(
                update, context,
                "Проверь данные:\n\n"
                f"  Имя: {data['name']}\n"
                f"  Название: {data['display_name']}\n"
                f"  Токен: {data['token'][:10]}...\n"
                f"  Роль: {data['description']}\n"
                f"  Модель: {data['model']}\n"
                f"  Доступ: {access_desc}\n\n"
                "Создать? (да/нет)"
            )

        elif step == "confirm":
            answer = text.strip().lower()
            if answer in ("да", "yes", "y", "д"):
                self._wizard_state.pop(chat_id, None)
                await self._wizard_create(update, context, data)
            elif answer in ("нет", "no", "n", "н"):
                self._wizard_state.pop(chat_id, None)
                await self._reply(update, context, "Создание отменено.")
            else:
                await self._reply(update, context, "Напиши 'да' или 'нет':")

    async def _wizard_create(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        data: dict,
    ) -> None:
        """Финальный шаг визарда — создание агента и hot-reload."""
        chat_id = update.effective_chat.id

        try:
            # Если открытый доступ — пустой allowed_users в yaml
            allowed_users = data.get("allowed_users", []) or None
            if data.get("open_access"):
                allowed_users = None  # Пустой список = все могут

            self.fleet_runtime.manager.create_agent(
                name=data["name"],
                display_name=data["display_name"],
                bot_token=data["token"],
                description=data["description"],
                model=data["model"],
                allowed_users=allowed_users,
            )
        except (ValueError, FileExistsError) as e:
            await self._reply(update, context, f"Ошибка: {e}")
            return

        await self._reply(
            update, context,
            f"Агент '{data['name']}' создан. Запускаю..."
        )

        # Hot-reload
        ok, msg = await self.fleet_runtime.start_agent(data["name"])
        if ok:
            # Получить ссылку на нового бота
            invite_link = await self._get_bot_invite_link(data["token"])
            reply = (
                f"Готово! Агент '{data['display_name']}' запущен.\n\n"
                f"Ссылка для клиента:\n{invite_link}\n\n"
                f"Клиент нажмёт Start — и бот автоматически запомнит его ID."
            )
            await self._reply(update, context, reply)
        else:
            await self._reply(
                update, context,
                f"Агент создан, но не запустился: {msg}\n"
                "Попробуй /start_agent " + data["name"]
            )

    def _save_allowed_users(self, new_user_id: int, user_name: str) -> None:
        """Добавить user ID в agent.yaml (persist)."""
        try:
            import yaml as _yaml
            yaml_path = Path(self.agent.config_path)
            with open(yaml_path, encoding="utf-8") as f:
                config = _yaml.safe_load(f.read())
            users = config.get("allowed_users", [])
            if isinstance(users, list) and new_user_id not in users:
                users.append(new_user_id)
                config["allowed_users"] = users
                with open(yaml_path, "w", encoding="utf-8") as f:
                    _yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                logger.info(
                    f"Auto-registered user {new_user_id} ({user_name}) "
                    f"for agent '{self.agent.name}'"
                )
        except Exception as e:
            logger.error(f"Failed to save allowed_users: {e}")

    async def _get_bot_invite_link(self, bot_token: str) -> str:
        """Получить invite link для бота по токену."""
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{bot_token}/getMe",
                    timeout=10,
                )
                data = resp.json()
                if data.get("ok"):
                    username = data["result"].get("username", "")
                    if username:
                        return f"https://t.me/{username}"
        except Exception:
            pass
        return "(не удалось получить ссылку — найди бота в Telegram вручную)"

    async def _cmd_clone_agent(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Клонировать агента: /clone_agent source_name"""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        source_name = args.strip()
        if not source_name:
            # Показать список доступных агентов для клонирования
            agents = self.fleet_runtime.manager.list_agents()
            if not agents:
                await self._reply(update, context, "Нет агентов для клонирования.")
                return
            lines = ["Какого агента клонировать?\n"]
            for a in agents:
                lines.append(f"  /clone_agent {a['name']}  — {a['display_name']}")
            await self._reply(update, context, "\n".join(lines))
            return

        # Проверить что источник существует
        source_dir = self.fleet_runtime.root / "agents" / source_name
        if not source_dir.exists():
            await self._reply(update, context, f"Агент '{source_name}' не найден.")
            return

        # Запустить визард клонирования (сокращённый: имя, токен, доступ)
        chat_id = update.effective_chat.id
        self._wizard_state[chat_id] = {
            "step": "clone_name",
            "data": {"clone_from": source_name},
        }
        await self._reply(
            update, context,
            f"Клонирую агента '{source_name}'.\n"
            "Скопирую: SOUL.md, скиллы, модель, настройки dream/heartbeat.\n\n"
            "Шаг 1/4: Имя нового агента (латиницей).\n"
            "Отправь /cancel чтобы отменить."
        )

    async def _clone_wizard_handle(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> bool:
        """Обработать ввод визарда клонирования. Возвращает True если обработал."""
        chat_id = update.effective_chat.id
        state = self._wizard_state.get(chat_id)
        if not state or not state["step"].startswith("clone_"):
            return False

        step = state["step"]
        data = state["data"]

        if step == "clone_name":
            name = text.strip().lower()
            from .agent_manager import AGENT_NAME_RE
            if not AGENT_NAME_RE.match(name):
                await self._reply(update, context, "Имя должно быть латиницей. Попробуй ещё:")
                return True
            if (self.fleet_runtime.root / "agents" / name).exists():
                await self._reply(update, context, f"'{name}' уже существует. Другое имя:")
                return True
            data["name"] = name
            state["step"] = "clone_display"
            await self._reply(
                update, context,
                f"Имя: {name}\n\nШаг 2/4: Отображаемое имя (на русском)."
            )

        elif step == "clone_display":
            data["display_name"] = text.strip()
            state["step"] = "clone_token"
            await self._reply(
                update, context,
                f"Название: {data['display_name']}\n\n"
                "Шаг 3/4: Токен бота от @BotFather."
            )

        elif step == "clone_token":
            token = text.strip()
            from .agent_manager import BOT_TOKEN_RE
            if not BOT_TOKEN_RE.match(token):
                await self._reply(update, context, "Невалидный токен. Попробуй ещё:")
                return True
            data["token"] = token
            state["step"] = "clone_users"
            await self._reply(
                update, context,
                "Шаг 4/4: Для кого этот агент?\n\n"
                "- Перешли сообщение от клиента — ID автоматически\n"
                "- Введи Telegram ID (число)\n"
                "- 'все' — открытый доступ\n"
                "- 'я' — только ты"
            )

        elif step == "clone_users":
            user_ids = []
            input_text = text.strip().lower()

            if input_text in ("все", "all"):
                data["open_access"] = True
            elif input_text not in ("я", "me", "i"):
                for part in text.replace(",", " ").split():
                    if part.strip().isdigit():
                        user_ids.append(int(part.strip()))
                fwd_origin = getattr(update.message, "forward_origin", None)
                if fwd_origin and getattr(fwd_origin, "type", "") == "user":
                    user_ids.append(fwd_origin.sender_user.id)

            data["allowed_users"] = user_ids
            self._wizard_state.pop(chat_id, None)

            # Создать клон
            try:
                allowed = None if data.get("open_access") else (user_ids or [])
                self.fleet_runtime.manager.clone_agent(
                    source_name=data["clone_from"],
                    new_name=data["name"],
                    new_display_name=data["display_name"],
                    new_bot_token=data["token"],
                    allowed_users=allowed,
                )
            except (ValueError, FileExistsError) as e:
                await self._reply(update, context, f"Ошибка: {e}")
                return True

            await self._reply(
                update, context,
                f"Агент '{data['name']}' клонирован из '{data['clone_from']}'. Запускаю..."
            )

            ok, msg = await self.fleet_runtime.start_agent(data["name"])
            if ok:
                invite_link = await self._get_bot_invite_link(data["token"])
                await self._reply(
                    update, context,
                    f"Готово! '{data['display_name']}' запущен.\n\n"
                    f"Ссылка для клиента:\n{invite_link}\n\n"
                    f"Клиент нажмёт Start — бот запомнит его ID."
                )
            else:
                await self._reply(
                    update, context,
                    f"Клонирован, но не запустился: {msg}\n"
                    f"Попробуй /start_agent {data['name']}"
                )

        return True

    async def _cmd_set_access(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Управление доступом: /set_access agent_name [user_id | forward | all | lock]"""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        parts = args.strip().split(None, 1)
        if not parts:
            # Показать справку
            agents = self.fleet_runtime.manager.list_agents()
            lines = [
                "Управление доступом к агентам.\n",
                "Использование:",
                "  /set_access имя_агента — показать текущий доступ",
                "  /set_access имя_агента 123456 — добавить user ID",
                "  /set_access имя_агента all — открыть для всех",
                "  /set_access имя_агента lock — только владелец",
                "  Или перешли сообщение от клиента с командой:\n"
                "  /set_access имя_агента + переслать сообщение\n",
            ]
            if agents:
                lines.append("Агенты:")
                for a in agents:
                    lines.append(f"  {a['name']} — {a['display_name']}")
            await self._reply(update, context, "\n".join(lines))
            return

        agent_name = parts[0]
        agent_yaml_path = self.fleet_runtime.root / "agents" / agent_name / "agent.yaml"

        if not agent_yaml_path.exists():
            await self._reply(update, context, f"Агент '{agent_name}' не найден.")
            return

        # Прочитать текущий конфиг
        import yaml as _yaml
        with open(agent_yaml_path, encoding="utf-8") as f:
            raw = f.read()
        config = _yaml.safe_load(raw)
        current_users = config.get("allowed_users", [])

        # Только показать текущий доступ
        if len(parts) == 1:
            # Проверить forwarded message
            fwd_origin = getattr(update.message, "forward_origin", None)
            if fwd_origin and getattr(fwd_origin, "type", "") == "user":
                # Добавить ID из пересланного сообщения
                fwd_user = fwd_origin.sender_user
                new_id = fwd_user.id
                if current_users and new_id not in current_users:
                    current_users.append(new_id)
                    config["allowed_users"] = current_users
                    with open(agent_yaml_path, "w", encoding="utf-8") as f:
                        _yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                    await self._reply(
                        update, context,
                        f"Добавлен доступ: {new_id} ({fwd_user.first_name or ''}) → '{agent_name}'\n"
                        f"Перезапусти агента: /stop_agent {agent_name} → /start_agent {agent_name}"
                    )
                    return
                elif not current_users:
                    await self._reply(update, context, f"'{agent_name}' уже открыт для всех.")
                    return
                else:
                    await self._reply(update, context, f"ID {new_id} уже в списке доступа.")
                    return

            # Показать текущий доступ + ссылку на бота
            if not current_users:
                access = "открытый (все могут писать)"
            else:
                import os
                founder_id = int(os.environ.get("FOUNDER_TELEGRAM_ID", "0") or "0")
                clients = [uid for uid in current_users if uid != founder_id]
                if clients:
                    access = "привязан к: " + ", ".join(str(uid) for uid in clients)
                else:
                    access = "только владелец (клиент не привязан)"

            # Получить ссылку на бота
            bot_token = config.get("bot_token", "")
            # Раскрыть ${VAR}
            if "${" in bot_token:
                import os as _os
                bot_token = _os.path.expandvars(bot_token)

            link = ""
            if bot_token and "${" not in bot_token:
                link = await self._get_bot_invite_link(bot_token)

            lines = [
                f"Агент: {agent_name}",
                f"Доступ: {access}",
            ]
            if link and not link.startswith("("):
                lines.append(f"\nСсылка для клиента:\n{link}")
                lines.append("Первый кто нажмёт Start — получит доступ.")
            lines.append(f"\nКоманды:")
            lines.append(f"  /set_access {agent_name} lock — сбросить привязку")
            lines.append(f"  /set_access {agent_name} all — открыть для всех")

            await self._reply(update, context, "\n".join(lines))
            return

        action = parts[1].strip()

        # Добавить user ID
        if action.isdigit():
            new_id = int(action)
            if not current_users:
                current_users = [new_id]
            elif new_id not in current_users:
                current_users.append(new_id)
            else:
                await self._reply(update, context, f"ID {new_id} уже в списке.")
                return
            config["allowed_users"] = current_users

        # Открыть для всех
        elif action in ("all", "все", "open"):
            config["allowed_users"] = []

        # Только владелец
        elif action in ("lock", "закрыть", "only_me"):
            import os
            founder_id = os.environ.get("FOUNDER_TELEGRAM_ID", "")
            config["allowed_users"] = [int(founder_id)] if founder_id.isdigit() else []

        else:
            await self._reply(
                update, context,
                f"Неизвестное действие: {action}\n"
                "Варианты: число (ID), all, lock"
            )
            return

        # Сохранить
        with open(agent_yaml_path, "w", encoding="utf-8") as f:
            _yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        if not config["allowed_users"]:
            result = "открытый доступ (все)"
        else:
            result = ", ".join(str(uid) for uid in config["allowed_users"])

        await self._reply(
            update, context,
            f"Доступ к '{agent_name}' обновлён: {result}\n"
            f"Перезапусти: /stop_agent {agent_name} → /start_agent {agent_name}"
        )

    async def _cmd_stop_agent(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Остановить агента: /stop_agent name"""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        name = args.strip()
        if not name:
            await self._reply(
                update, context,
                "Укажи имя агента: /stop_agent <имя>\n"
                "Список: /agents"
            )
            return

        # Нельзя остановить самого себя
        if name == self.agent.name:
            await self._reply(update, context, "Нельзя остановить самого себя.")
            return

        ok, msg = await self.fleet_runtime.stop_agent(name)
        await self._reply(update, context, msg)

    async def _cmd_start_agent(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Запустить агента: /start_agent name"""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        name = args.strip()
        if not name:
            await self._reply(
                update, context,
                "Укажи имя агента: /start_agent <имя>\n"
                "Список: /agents"
            )
            return

        ok, msg = await self.fleet_runtime.start_agent(name)
        await self._reply(update, context, msg)

    # ── Skill Creator commands ──

    async def _cmd_skills(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Показать список скиллов агента: /skills [agent_name]"""
        from .skill_creator import list_skills, get_all_agent_dirs

        target_name = args.strip() if args.strip() else self.agent.name

        # Найти директорию целевого агента
        if target_name == self.agent.name:
            agent_dir = self.agent.agent_dir
        elif self.fleet_runtime and target_name in self.fleet_runtime.agents:
            agent_dir = self.fleet_runtime.agents[target_name].agent_dir
        else:
            # Попробовать найти по файловой системе
            agents = get_all_agent_dirs(
                str(Path(self.agent.agent_dir).parent.parent)
            )
            agent_dir = agents.get(target_name)

        if not agent_dir:
            await self._reply(
                update, context,
                f"Агент '{target_name}' не найден."
            )
            return

        skills = list_skills(agent_dir)
        if not skills:
            await self._reply(
                update, context,
                f"У агента '{target_name}' нет скиллов.\n"
                f"Создай: /newskill описание скилла"
            )
            return

        lines = [f"Скиллы агента '{target_name}':"]
        for s in skills:
            always_tag = " [always]" if s["always"] else ""
            lines.append(f"  - {s['name']}{always_tag}: {s['description']}")
        lines.append(f"\nВсего: {len(skills)}")

        await self._reply(update, context, "\n".join(lines))

    async def _cmd_newskill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Создать новый скилл: /newskill описание"""
        # Только master может создавать скиллы
        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Создание скиллов доступно только master-агенту."
            )
            return

        description = args.strip()
        if not description:
            await self._reply(
                update, context,
                "Опиши скилл: /newskill <описание>\n\n"
                "Примеры:\n"
                "  /newskill анализ конкурентов — исследование и сравнение\n"
                "  /newskill ежедневный отчёт по задачам команды\n"
                "  /newskill генерация SQL запросов из текстового описания"
            )
            return

        # Определить целевого агента
        # Формат: /newskill @agent_name описание
        # или просто /newskill описание (создаётся для текущего агента)
        target_name = self.agent.name
        target_dir = self.agent.agent_dir

        if description.startswith("@"):
            parts = description.split(maxsplit=1)
            candidate = parts[0][1:]  # убрать @
            if self.fleet_runtime and candidate in self.fleet_runtime.agents:
                target_name = candidate
                target_dir = self.fleet_runtime.agents[candidate].agent_dir
                description = parts[1] if len(parts) > 1 else ""
                if not description:
                    await self._reply(
                        update, context,
                        f"Укажи описание скилла для @{target_name}:\n"
                        f"/newskill @{target_name} <описание>"
                    )
                    return

        from .skill_creator import create_skill

        chat_id = update.effective_chat.id
        status = StatusMessage(chat_id, context)
        await status.show("Генерирую скилл...")

        try:
            # Определить роль целевого агента
            if self.fleet_runtime and target_name in self.fleet_runtime.agents:
                target_role = self.fleet_runtime.agents[target_name].role
            else:
                target_role = "worker"

            ok, message = await create_skill(
                user_request=description,
                agent_dir=target_dir,
                agent_name=target_name,
                agent_role=target_role,
                model="sonnet",
            )

            await status.cleanup()
            await self._reply(update, context, message)

        except Exception as e:
            await status.cleanup()
            logger.error(f"NewSkill command error: {e}")
            await self._reply(
                update, context,
                f"Ошибка создания скилла: {e}"
            )

    async def _cmd_removeskill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Удалить скилл: /removeskill skill_name [@agent_name]"""
        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Удаление скиллов доступно только master-агенту."
            )
            return

        parts = args.strip().split()
        if not parts:
            await self._reply(
                update, context,
                "Укажи имя скилла: /removeskill <skill_name> [@agent_name]\n"
                "Список скиллов: /skills"
            )
            return

        skill_name = parts[0]

        # Определить целевого агента
        target_dir = self.agent.agent_dir
        target_name = self.agent.name
        if len(parts) > 1 and parts[1].startswith("@"):
            candidate = parts[1][1:]
            if self.fleet_runtime and candidate in self.fleet_runtime.agents:
                target_name = candidate
                target_dir = self.fleet_runtime.agents[candidate].agent_dir
            else:
                await self._reply(
                    update, context,
                    f"Агент '{candidate}' не найден."
                )
                return

        from .skill_creator import remove_skill

        ok = remove_skill(skill_name, target_dir)
        if ok:
            await self._reply(
                update, context,
                f"Скилл '{skill_name}' удалён у агента '{target_name}'."
            )
        else:
            await self._reply(
                update, context,
                f"Скилл '{skill_name}' не найден у агента '{target_name}'.\n"
                f"Список: /skills {target_name}"
            )

    # ── Skill Pool commands (маркетплейс) ──

    async def _cmd_poolskills(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Показать каталог скиллов из пула: /poolskills"""
        pool = self._get_skill_pool()
        if pool is None:
            await self._reply(
                update, context,
                "Пул скиллов не настроен.\n"
                "Задай SKILL_POOL_URL в .env (см .env.example) "
                "и перезапусти бота."
            )
            return

        # Автоматически обновляем пул если его ещё нет на диске
        if not pool.is_available():
            try:
                pool.refresh()
            except Exception as e:
                await self._reply(
                    update, context,
                    f"Ошибка при клонировании пула: {e}\n"
                    f"Проверь SKILL_POOL_URL и доступность репо."
                )
                return

        try:
            skills = pool.list_skills()
        except Exception as e:
            await self._reply(
                update, context,
                f"Ошибка чтения manifest.json: {e}"
            )
            return

        if not skills:
            await self._reply(
                update, context,
                "В пуле пока нет опубликованных скиллов."
            )
            return

        lines = ["Каталог скиллов из пула:\n"]
        for s in skills:
            tags = " ".join(f"#{t}" for t in s.tags) if s.tags else ""
            mem_note = ""
            if s.requires_memory:
                mem_note = (
                    f"\n    требует память: {', '.join(s.requires_memory)}"
                )

            # Маркеры типа и скриптов перед именем
            type_mark = "📦" if s.type == "bundle" else "📄"
            scripts_mark = " ⚠️ скрипты" if s.has_scripts else ""

            lines.append(
                f"• {type_mark} *{s.name}* v{s.version}{scripts_mark} — "
                f"{s.description}{mem_note}"
            )
            if tags:
                lines.append(f"    {tags}")

        # Легенда для пользователя
        lines.append(
            f"\nВсего: {len(skills)}"
            f"\n📄 — single-file скилл (только markdown)"
            f"\n📦 — bundle (директория с доп. файлами)"
            f"\n⚠️ скрипты — содержит исполняемый код (Python/Bash/JS)"
            f"\n\nУстановить: /installskill <имя> [@agent]"
        )
        await self._reply(update, context, "\n".join(lines))

    async def _cmd_installskill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Установить скилл из пула: /installskill <имя> [@agent]"""
        # Только master может устанавливать скиллы
        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Установка скиллов доступна только master-агенту."
            )
            return

        parts = args.strip().split()
        if not parts:
            await self._reply(
                update, context,
                "Укажи имя скилла: /installskill <имя> [@agent]\n"
                "Каталог: /poolskills"
            )
            return

        skill_name = parts[0]

        # Определить целевого агента
        target_name = self.agent.name
        target_dir = Path(self.agent.agent_dir)
        if len(parts) > 1 and parts[1].startswith("@"):
            candidate = parts[1][1:]
            if self.fleet_runtime and candidate in self.fleet_runtime.agents:
                target_name = candidate
                target_dir = Path(
                    self.fleet_runtime.agents[candidate].agent_dir
                )
            else:
                await self._reply(
                    update, context,
                    f"Агент '{candidate}' не найден."
                )
                return

        pool = self._get_skill_pool()
        if pool is None:
            await self._reply(
                update, context,
                "Пул скиллов не настроен (SKILL_POOL_URL не задан)."
            )
            return

        if not pool.is_available():
            try:
                pool.refresh()
            except Exception as e:
                await self._reply(
                    update, context,
                    f"Ошибка при обновлении пула: {e}"
                )
                return

        result = pool.install_skill(skill_name, target_dir)

        if not result.ok:
            await self._reply(
                update, context,
                f"Не удалось установить '{skill_name}': {result.error}"
            )
            return

        msg_lines = [
            f"Скилл '{skill_name}' установлен агенту '{target_name}'.",
            f"Путь: {result.installed_to}",
        ]

        if result.has_scripts:
            msg_lines.append(
                "\n⚠️ Скилл содержит исполняемые скрипты (Python/Bash/JS). "
                "Они скопированы вместе со скиллом и могут запускаться "
                "Claude Agent SDK по запросу. Убедись что доверяешь автору "
                "перед реальным использованием."
            )

        if result.missing_memory:
            msg_lines.append(
                f"\nСкилл декларирует файлы памяти, которых пока нет у агента:\n"
                + "\n".join(f"  - {m}" for m in result.missing_memory)
                + "\n\nСкилл будет работать, но пока не сможет читать из них. "
                  "Создай эти файлы через обычный диалог с агентом — "
                  "он сам их заполнит когда ты ответишь на его вопросы."
            )

        await self._reply(update, context, "\n".join(msg_lines))

    async def _cmd_refreshpool(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Обновить кэш пула скиллов: /refreshpool"""
        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Обновление пула доступно только master-агенту."
            )
            return

        pool = self._get_skill_pool()
        if pool is None:
            await self._reply(
                update, context,
                "Пул скиллов не настроен (SKILL_POOL_URL не задан)."
            )
            return

        try:
            pool.refresh()
        except Exception as e:
            await self._reply(
                update, context,
                f"Ошибка обновления пула: {e}"
            )
            return

        try:
            skills = pool.list_skills()
            await self._reply(
                update, context,
                f"Пул обновлён. Доступно скиллов: {len(skills)}.\n"
                f"Каталог: /poolskills"
            )
        except Exception as e:
            await self._reply(
                update, context,
                f"Пул склонирован, но manifest.json не читается: {e}"
            )

    # ── Mini App dashboard ──

    async def _cmd_dashboard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Открыть Telegram Mini App с дэшбордом."""
        url = os.environ.get("MINIAPP_URL", "").strip()
        if not url:
            await self._reply(
                update, context,
                "Дэшборд ещё не настроен. Администратор должен "
                "задать MINIAPP_URL в .env (требуется HTTPS).",
            )
            return
        if not url.startswith("https://"):
            await self._reply(
                update, context,
                "Дэшборд требует HTTPS-URL (ограничение Telegram WebApp).",
            )
            return

        separator = "&" if "?" in url else "?"
        launch_url = f"{url}{separator}origin_agent={self.agent.name}"
        button = InlineKeyboardButton(
            "Открыть дэшборд", web_app=WebAppInfo(url=launch_url)
        )
        await update.effective_message.reply_text(
            "Mini App агента «{}»:".format(self.agent.display_name),
            reply_markup=InlineKeyboardMarkup([[button]]),
        )

    async def _cmd_setup_dashboard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """One-click настройка дэшборда: nginx + HTTPS + меню. Только master."""
        from .miniapp import setup_flow

        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Эта команда доступна только master-агенту."
            )
            return

        tokens = args.strip().split()
        if not tokens:
            await self._reply(
                update, context,
                "Использование: /setup_dashboard <domain> [email]\n"
                "Пример: /setup_dashboard bot.example.com you@example.com\n\n"
                "Что произойдёт:\n"
                "• Проверю DNS домена\n"
                "• Подберу свободный порт\n"
                "• Настрою nginx и выпущу HTTPS через Let's Encrypt\n"
                "• Обновлю .env и активирую кнопку меню Mini App\n"
                "• Перезапущу бота"
            )
            return

        try:
            domain = setup_flow.validate_domain(tokens[0])
        except ValueError as e:
            await self._reply(update, context, f"Невалидный домен: {e}")
            return

        email: str | None = None
        if len(tokens) >= 2:
            try:
                email = setup_flow.validate_email(tokens[1])
            except ValueError as e:
                await self._reply(update, context, f"Невалидный email: {e}")
                return

        if not await setup_flow.sudoers_ready():
            await self._reply(
                update, context,
                "Не могу запустить helper: sudo NOPASSWD не настроен.\n"
                "Включите один раз на сервере:\n"
                "    ./setup.sh --enable-dashboard\n"
                "и повторите /setup_dashboard."
            )
            return

        await self._reply(update, context, f"Проверяю DNS для {domain}…")
        try:
            ip = await setup_flow.check_dns(domain)
        except ValueError as e:
            await self._reply(
                update, context,
                f"{e}\n\nПроверьте что A-запись домена указывает на этот "
                "сервер и DNS успел распространиться."
            )
            return

        await self._reply(update, context, f"DNS OK → {ip}. Подбираю порт…")
        try:
            port = setup_flow.pick_free_port()
        except RuntimeError as e:
            await self._reply(update, context, f"Не нашёл свободный порт: {e}")
            return

        await self._reply(
            update, context,
            f"Порт {port}. Настраиваю nginx + certbot… (до 30 секунд)"
        )

        ok, output = await setup_flow.run_setup_helper(domain, port, email)
        if not ok:
            tail = "\n".join(output.splitlines()[-10:]) or "(нет вывода)"
            await self._reply(
                update, context,
                f"Helper завершился с ошибкой. Последние строки:\n\n{tail}"
            )
            return

        try:
            setup_flow.update_env_file(
                setup_flow.ENV_PATH,
                {
                    "HTTP_PORT": str(port),
                    "HTTP_HOST": "127.0.0.1",
                    "PUBLIC_BASE_URL": f"https://{domain}",
                    "MINIAPP_URL": f"https://{domain}/miniapp/",
                },
            )
        except OSError as e:
            await self._reply(update, context, f"Не смог записать .env: {e}")
            return

        menu_url = f"https://{domain}/miniapp/?origin_agent={self.agent.name}"
        try:
            await context.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Cockpit",
                    web_app=WebAppInfo(url=menu_url),
                )
            )
        except Exception as e:
            logger.warning(f"set_chat_menu_button failed: {e}")

        await self._reply(
            update, context,
            f"✅ Готово: https://{domain}/miniapp/\n"
            "Кнопка меню «Cockpit» активна. Перезапускаюсь…"
        )
        await setup_flow.trigger_restart_detached()

    # ── GitHub Backup commands ──

    async def _cmd_backup_link(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Показать ссылку на GitHub бэкап: /backup_link"""
        from .github_sync import get_backup_url
        project_root = Path(self.agent.agent_dir).parent.parent
        url = get_backup_url(project_root)
        if not url:
            await self._reply(
                update, context,
                "GitHub Sync не настроен. Добавь GITHUB_SYNC_REPO в .env"
            )
            return
        await self._reply(update, context, f"Ссылка на бэкап:\n{url}")

    async def _cmd_backup_now(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Запустить бэкап прямо сейчас: /backup_now (только для founder)"""
        founder_id = int(os.environ.get("FOUNDER_TELEGRAM_ID", "0") or "0")
        user_id = update.effective_user.id if update.effective_user else 0
        if founder_id and user_id != founder_id:
            await self._reply(update, context, "Только владелец может запускать бэкап вручную.")
            return

        from .github_sync import run_github_sync
        project_root = Path(self.agent.agent_dir).parent.parent

        await self._reply(update, context, "Запускаю бэкап... (займёт ~30 секунд)")
        result = await run_github_sync(project_root)

        if result["success"]:
            agents_str = ", ".join(result.get("agents", []))
            await self._reply(
                update, context,
                f"{result['message']}\n"
                f"Агенты: {agents_str}\n"
                f"Время: {result.get('timestamp', '')}\n"
                f"Ссылка: {result.get('repo_url', '')}"
            )
        else:
            await self._reply(update, context, f"Ошибка бэкапа: {result['message']}")

    async def _cmd_backup_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        """Показать статус последнего бэкапа: /backup_status"""
        from .github_sync import get_last_sync_info, get_backup_url
        project_root = Path(self.agent.agent_dir).parent.parent
        info = get_last_sync_info(project_root)
        url = get_backup_url(project_root)

        if not info:
            repo_configured = bool(os.getenv("GITHUB_SYNC_REPO"))
            if not repo_configured:
                await self._reply(
                    update, context,
                    "GitHub Sync не настроен. Добавь GITHUB_SYNC_REPO и GITHUB_SYNC_TOKEN в .env"
                )
            else:
                await self._reply(
                    update, context,
                    "Бэкап ещё не выполнялся. Следующий — сегодня в 03:00 UTC.\n"
                    "Или запусти сейчас: /backup_now"
                )
            return

        agents_str = ", ".join(info.get("agents", []))
        await self._reply(
            update, context,
            f"Последний бэкап: {info.get('last_sync', 'неизвестно')}\n"
            f"Агенты: {agents_str}\n"
            f"Ссылка: {url or 'не настроено'}"
        )

    # ── Message aggregation ──

    async def _flush_buffer(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Отправить накопленные сообщения в Claude после задержки."""
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

        # Логируем входящее сообщение (в DM — в личный лог, в группах уже залогировано)
        if not is_group:
            memory.log_message(
                user_dir, "user", "\n".join(messages), files or None
            )

        # ── Режим MessageBus ──
        if self.bus:
            from .bus import FleetMessage, MessageType

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
            from .file_handler import scan_outbox
            outbox_files = scan_outbox(user_dir)

            # Финализация: edit на месте если корот��о и нет файлов
            finalized = False
            if not outbox_files:
                finalized = await status.finalize(response)
            else:
                await status.cleanup()

            # Ес��и edit не удался — отправить новым сообщением
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
        self,
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

    def _is_group_chat(self, update: Update) -> bool:
        """Проверить, является ли чат групповым."""
        chat_type = update.effective_chat.type if update.effective_chat else ""
        return chat_type in ("group", "supergroup")

    def _is_bot_mentioned(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
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

    def _is_topic_allowed(self, chat_id: int, thread_id: int | None) -> bool:
        """Проверить, разрешён ли топик для ответа. True = можно отвечать."""
        allowed = memory.get_group_setting(
            self.agent.agent_dir, chat_id, "allowed_topic"
        )
        if allowed is None:
            return True  # Нет ограничения — отвечаем везде
        if thread_id is None:
            return False  # Есть ограничение, но сообщение не в топике
        return thread_id == allowed

    async def _handle_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
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

        # Добавить имя отправителя (важно для групп)
        if is_group:
            sender_name = self._get_sender_name(update)
            text = f"[{sender_name}]: {text}"

        self._add_to_buffer(chat_id, text, None, context, user_id=update.effective_user.id if update.effective_user else None)

    async def _handle_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
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
            self._add_to_buffer(chat_id, caption, file_path, context, user_id=update.effective_user.id if update.effective_user else None)
        except Exception as e:
            logger.error(f"File download error: {e}")
            await update.message.reply_text("Не удалось скачать файл.")

    async def _handle_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
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
            self._add_to_buffer(chat_id, caption, file_path, context, user_id=update.effective_user.id if update.effective_user else None)
        except Exception as e:
            logger.error(f"Photo download error: {e}")
            await update.message.reply_text("Не удалось скачать фото.")

    async def _handle_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
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
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
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

    async def _on_bot_added_to_group(self, chat, context) -> None:
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
        self, group_chat_id: int, chat_title: str, context
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
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
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

    # ── Bus Listener ──

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
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=formatted_part,
                    parse_mode=parse_mode,
                    message_thread_id=message_thread_id,
                )
            except Exception as e:
                if parse_mode:
                    logger.warning(
                        f"HTML send failed for chat {chat_id}: {e}\n"
                        f"HTML preview: {formatted_part[:300]!r}"
                    )
                    try:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=part,
                            message_thread_id=message_thread_id,
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
