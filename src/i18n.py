"""
i18n — локализация системных сообщений бота.

Поддерживает: ru (русский), en (English).
Язык хранится в settings.json каждого пользователя (ключ "language").
"""

# Словарь: ключ → {lang: текст}
_STRINGS: dict[str, dict[str, str]] = {
    # ── Онбординг ──
    "lang_select": {
        "en": "Choose your language:",
        "ru": "Выбери язык общения:",
    },
    "onboarding_prompt": {
        "en": (
            "[SYSTEM COMMAND: ONBOARDING]\n"
            "This is the first launch. The user hasn't filled in their profile yet.\n"
            "Greet the user, introduce yourself, and ask them to tell about themselves.\n"
            "Ask these questions:\n"
            "1. What is your name?\n"
            "2. What do you do, what company/project?\n"
            "3. What are your current main tasks and priorities?\n"
            "4. Who are the key people on your team?\n"
            "5. Your timezone?\n"
            "6. (Optional) If you'd like to use voice messages — "
            "send a Deepgram API key (get one free at console.deepgram.com). "
            "You can skip this and add it later.\n\n"
            "Ask everything in one friendly message. "
            "When the user replies — save everything to profile.md via Write.\n"
            "If the user sends a Deepgram key — save it "
            'to settings.json via Write in format: '
            '{"deepgram_api_key": "key"}\n'
            "Response format: warm greeting + numbered questions."
        ),
        "ru": (
            "[СИСТЕМНАЯ КОМАНДА: ОНБОРДИНГ]\n"
            "Это первый запуск. Пользователь ещё не заполнил свой профиль.\n"
            "Поприветствуй пользователя, представься и попроси рассказать о себе.\n"
            "Задай вопросы:\n"
            "1. Как тебя зовут?\n"
            "2. Чем занимаешься, какая компания/проект?\n"
            "3. Какие сейчас главные задачи и приоритеты?\n"
            "4. Кто ключевые люди в команде?\n"
            "5. Часовой пояс?\n"
            "6. (Необязательно) Если хочешь общаться голосовыми — "
            "пришли API-ключ Deepgram (получить бесплатно на console.deepgram.com). "
            "Можно пропустить и добавить позже.\n\n"
            "Спроси всё в одном дружелюбном сообщении. "
            "Когда пользователь ответит — запиши всё в profile.md через Write.\n"
            "Если пользователь прислал ключ Deepgram — сохрани его "
            "в settings.json через Write в формате: "
            '{"deepgram_api_key": "ключ"}\n'
            "Формат ответа: тёплое приветствие + вопросы списком."
        ),
    },
    "onboarding_fallback": {
        "en": (
            "Hi! I'm {display_name}.\n\n"
            "Tell me a bit about yourself so I can help you better:\n"
            "- What's your name?\n"
            "- What do you do?\n"
            "- What are your current priorities?\n"
            "- Who's on your team?"
        ),
        "ru": (
            "Привет! Я — {display_name}.\n\n"
            "Расскажи немного о себе, чтобы я мог лучше помогать:\n"
            "- Как тебя зовут?\n"
            "- Чем занимаешься?\n"
            "- Какие сейчас приоритеты?\n"
            "- Кто в команде?"
        ),
    },
    "onboarding_save_instruction": {
        "en": (
            "\n\n[SYSTEM COMMAND: the user answered the onboarding questions. "
            "Save the information to profile.md via the Write tool. "
            "Replace all [fill in] placeholders with real data. "
            "After saving, confirm to the user that everything is remembered.]"
        ),
        "ru": (
            "\n\n[СИСТЕМНАЯ КОМАНДА: пользователь ответил на вопросы онбординга. "
            "Запиши полученную информацию в profile.md через инструмент Write. "
            "Замени все плейсхолдеры [заполни] реальными данными. "
            "После записи подтверди пользователю что запомнил всё.]"
        ),
    },

    # ── /start ──
    "start_greeting": {
        "en": "Hi! I'm {display_name}.\nSend me a message and I'll help.",
        "ru": "Привет! Я — {display_name}.\nНапиши мне что-нибудь, и я помогу.",
    },

    # ── /help ──
    "help_text": {
        "en": (
            "Available commands:\n"
            "/start — greeting\n"
            "/help — this help\n"
            "/newsession — new session (reset context)\n"
            "/stop — stop current request\n"
            "/status — agent status\n"
            "/memory — memory change history\n"
            "/restore — roll back memory\n"
            "/dream — run Dream memory processing\n\n"
            "Agent management:\n"
            "/agents — list all agents\n"
            "/create_agent — create a new agent\n"
            "/start_agent — start an agent\n"
            "/stop_agent — stop an agent\n\n"
            "Skills:\n"
            "/skills — list agent skills\n"
            "/newskill — create a new skill\n"
            "/removeskill — remove a skill\n\n"
            "System:\n"
            "/restart — restart platform (applies code updates)\n\n"
            "Or press a button below:"
        ),
        "ru": (
            "Доступные команды:\n"
            "/start — приветствие\n"
            "/help — эта справка\n"
            "/newsession — новая сессия (сброс контекста)\n"
            "/stop — остановить текущий запрос\n"
            "/status — статус агента\n"
            "/memory — история изменений памяти\n"
            "/restore — откатить память\n"
            "/dream — запустить Dream-обработку памяти\n\n"
            "Управление агентами:\n"
            "/agents — список всех агентов\n"
            "/create_agent — создать нового агента\n"
            "/start_agent — запустить агента\n"
            "/stop_agent — остановить агента\n\n"
            "Скиллы:\n"
            "/skills — список скиллов агента\n"
            "/newskill — создать новый скилл\n"
            "/removeskill — удалить скилл\n\n"
            "Система:\n"
            "/restart — перезапуск платформы (применяет обновления кода)\n\n"
            "Или нажми кнопку ниже:"
        ),
    },

    # ── Status messages ──
    "thinking": {
        "en": "Thinking...",
        "ru": "Думаю...",
    },
    "starting": {
        "en": "Starting...",
        "ru": "Запускаюсь...",
    },
    "new_session": {
        "en": "New session started. Previous context has been reset.",
        "ru": "Новая сессия начата. Контекст предыдущей сессии сброшен.",
    },
    "stopped": {
        "en": "Stopped.",
        "ru": "Остановлено.",
    },
    "no_active_request": {
        "en": "No active request.",
        "ru": "Нет активного запроса.",
    },
    "unknown_command": {
        "en": "Unknown command. /help for the list.",
        "ru": "Неизвестная команда. /help для списка.",
    },
    "owner_only": {
        "en": "This command is only available to the owner.",
        "ru": "Эта команда доступна только владельцу.",
    },
    "error_generic": {
        "en": "An error occurred. Try again.",
        "ru": "Произошла ошибка. Попробуй ещё раз.",
    },
    "error_timeout": {
        "en": "Response took too long. Try rephrasing.",
        "ru": "Ответ занял слишком долго. Попробуй переформулировать.",
    },
    "file_too_large": {
        "en": "File is too large (max 20MB).",
        "ru": "Файл слишком большой (макс. 20MB).",
    },
    "file_download_error": {
        "en": "Failed to download the file.",
        "ru": "Не удалось скачать файл.",
    },
    "photo_download_error": {
        "en": "Failed to download the photo.",
        "ru": "Не удалось скачать фото.",
    },
    "voice_not_configured": {
        "en": (
            "Voice messages are not set up yet.\n"
            "Send me a Deepgram API key to enable voice recognition.\n"
            "Get a key: https://console.deepgram.com/"
        ),
        "ru": (
            "Голосовые сообщения пока не настроены.\n"
            "Отправь мне ключ Deepgram API — и я включу распознавание голоса.\n"
            "Получить ключ: https://console.deepgram.com/"
        ),
    },
    "voice_error": {
        "en": "Failed to process voice message.",
        "ru": "Не удалось обработать голосовое сообщение.",
    },

    # ── Group ──
    "group_greeting": {
        "en": (
            "Hi! I'm {display_name}.\n\n"
            "I'll keep track of the conversation context. "
            "Mention @{bot_username} when you need help."
        ),
        "ru": (
            "Привет! Я — {display_name}.\n\n"
            "Буду следить за контекстом беседы. "
            "Упомяните @{bot_username} когда нужна помощь."
        ),
    },
    "group_added_dm": {
        "en": (
            "I've been added to the group \"{chat_title}\".\n\n"
            "How should I behave there? Press \"Configure\" and describe "
            "the rules — for example, role, tone, topics, restrictions."
        ),
        "ru": (
            "Меня добавили в группу «{chat_title}».\n\n"
            "Как мне себя там вести? Нажми «Настроить» и опиши "
            "правила — например, роль, тон, темы, ограничения."
        ),
    },
    "group_forum_hint": {
        "en": (
            "\n\nI see this group has topics enabled. "
            "I can respond in all topics or just one — choose below."
        ),
        "ru": (
            "\n\nВижу, что в группе включены темы (топики). "
            "Могу отвечать во всех или только в одной — выбери ниже."
        ),
    },
    "group_rules_saved": {
        "en": "Done! Rules for the group are saved.",
        "ru": "Готово! Правила для группы сохранены.",
    },
    "group_setup_prompt": {
        "en": (
            "Describe how I should behave in this group.\n\n"
            "For example: role, tone, topics, restrictions. "
            "Just type it in the next message."
        ),
        "ru": (
            "Опиши, как мне вести себя в этой группе.\n\n"
            "Например: роль, тон общения, темы, ограничения. "
            "Просто напиши текстом в следующем сообщении."
        ),
    },
    "group_skip": {
        "en": (
            "OK, I'll use default behavior. "
            "You can configure later by sending me the rules."
        ),
        "ru": (
            "Ок, буду вести себя по умолчанию. "
            "Настроить можно позже — отправь мне правила и "
            "укажи для какой группы."
        ),
    },

    # ── Buttons ──
    "btn_configure": {"en": "Configure", "ru": "Настроить"},
    "btn_skip": {"en": "Skip", "ru": "Пропустить"},
    "btn_one_topic": {"en": "One topic only", "ru": "Только одна тема"},
    "btn_all_topics": {"en": "All topics", "ru": "Все темы"},
    "btn_status": {"en": "Status", "ru": "Статус"},
    "btn_memory": {"en": "Memory", "ru": "Память"},
    "btn_new_session": {"en": "New session", "ru": "Новая сессия"},
    "btn_stop": {"en": "Stop", "ru": "Стоп"},
    "btn_restore": {"en": "Restore memory", "ru": "Откатить память"},
    "btn_model": {"en": "Model", "ru": "Модель"},

    # ── System prompt language instruction ──
    "system_lang_instruction": {
        "en": "\n\n## Language\nCommunicate with the user in English.",
        "ru": "\n\n## Язык\nОбщайся с пользователем на русском языке.",
    },
}

DEFAULT_LANG = "ru"


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """
    Получить локализованную строку.

    Args:
        key: ключ строки
        lang: язык ("en" или "ru"), по умолчанию "ru"
        **kwargs: подстановки для format()

    Returns:
        Локализованная строка
    """
    lang = lang or DEFAULT_LANG
    strings = _STRINGS.get(key, {})
    text = strings.get(lang) or strings.get(DEFAULT_LANG, f"[{key}]")
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text
