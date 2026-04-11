# Roadmap: My Claude Bot

## Источник идей: nanobot (HKUDS/nanobot)
Ультралёгкий AI-агент, 39k звёзд, MIT. Поддержка 12+ мессенджеров, Dream-память,
подагенты, cron, heartbeat, MCP, песочница, навыки как Markdown.

---

## Phase 1: Быстрые улучшения (текущий спринт)

### 1.1 Tool Hints — статус инструментов в Telegram
**Что:** Когда агент выполняет инструменты, пользователь видит реальный статус вместо "typing...":
- "Читаю config.py..."
- "Ищу 'error' в логах..."
- "Выполняю npm test..."

**Где:** Новый файл `src/tool_hints.py` + интеграция в `telegram_bridge.py`

**Как:** Словарь шаблонов для каждого инструмента, парсинг tool_use событий из Claude SDK.

**Статус:** [x] готово — `src/tool_hints.py`, интегрировано в `agent.py` и `telegram_bridge.py`

---

### 1.2 Stream Delta Coalescing — защита от rate limit Telegram
**Что:** Telegram ограничивает editMessageText (~30 раз/минуту на чат). Вместо edit на каждый
токен — буферизация чанков и отправка накопленного текста каждые 300-500мс.

**Где:** `src/telegram_bridge.py` — добавить `_stream_buffer` с таймером

**Как:**
- При получении streaming-чанка — добавить в буфер
- Таймер 400мс: если накопились чанки — один editMessageText
- При финальном чанке — flush буфера

**Статус:** [x] готово — класс `StatusMessage` в `telegram_bridge.py` с интервалом 0.5с

---

### 1.3 Command Router с приоритетами
**Что:** 4-уровневая маршрутизация команд:
- **Priority:** `/stop`, `/restart` — работают ДАЖЕ когда агент занят (до блокировки)
- **Exact:** `/newsession`, `/help`, `/dream`, `/status`
- **Prefix:** `/team coder задача` — делегирование агенту (Phase 2)
- **Interceptors:** fallback-обработчики

**Где:** Новый файл `src/command_router.py` + интеграция в `telegram_bridge.py`

**Зачем:** Критично для надёжности — пользователь должен мочь остановить зависшего агента.

**Статус:** [x] готово — `src/command_router.py`, команды: /stop, /start, /help, /newsession, /status, /memory, /restore

---

### 1.4 Git-backed memory — версионирование wiki
**Что:** Автоматический git-коммит файлов памяти после значимых изменений + команда
`/restore` для отката к предыдущей версии.

**Где:** Расширение `src/memory.py` через git CLI

**Как:**
- `git init` в `agents/{name}/memory/` при первом запуске
- Auto-commit после каждого ответа агента
- `/restore` — откат к предыдущему коммиту
- `/memory` — история изменений

**Статус:** [x] готово — `git_init`, `git_commit`, `git_log`, `git_restore` в `memory.py`

---

## Phase 2: Multi-Agent Fleet (после Phase 1)

### 2.1 MessageBus — центральная шина сообщений
**Что:** asyncio.Queue-based шина для развязки каналов и агентов.

**Как маппится:**
- Telegram handler публикует InboundMessage в шину
- Оркестратор читает из шины, определяет какому агенту передать
- Агент публикует OutboundMessage обратно
- Агенты могут общаться друг с другом через ту же шину

**Структура:**
```python
@dataclass
class FleetMessage:
    source: str        # "telegram", "agent:coder", "system"
    target: str        # "orchestrator", "agent:coder", "telegram"
    session_id: str
    content: str
    metadata: dict

class FleetBus:
    queues: dict[str, asyncio.Queue]
    async def publish(msg: FleetMessage)
    async def consume(name: str) -> FleetMessage
```

**Статус:** [x] готово — `src/bus.py` (FleetMessage, FleetBus с pub/sub routing)

---

### 2.2 Dream Memory — фоновая обработка памяти
**Что:** Двухфазная "сонная" обработка каждые 2 часа:
- Phase 1: Claude анализирует необработанные сообщения, извлекает факты
- Phase 2: Claude с инструментами Read/Edit хирургически правит wiki/, profile.md, index.md

**Как маппится на Karpathy Wiki:**
- Cursor-трекинг: позиция последней обработанной записи в history
- Phase 1 промпт: "извлеки ключевые факты, решения, предпочтения"
- Phase 2 промпт: "обнови wiki с этими фактами, делай хирургические правки"
- Git auto-commit после каждого Dream-цикла

**Конфиг в agent.yaml:**
```yaml
dream:
  interval_hours: 2
  phase1_prompt: "templates/dream_phase1.md"
  phase2_prompt: "templates/dream_phase2.md"
  tracked_files: ["memory/profile.md", "memory/wiki/", "memory/index.md"]
  git_autocommit: true
```

**Статус:** [x] готово — `src/dream.py`, шаблоны `agents/me/templates/dream_phase1.md`, `dream_phase2.md`, cursor-трекинг, команда `/dream`

---

### 2.3 Heartbeat + Evaluator — проактивные агенты
**Что:** Периодическая проверка задач + LLM-оценка "стоит ли уведомлять".

**Три LLM-вызова:**
1. "Есть задачи в HEARTBEAT.md?" (дешёвый вызов)
2. Выполнение задачи (полный агент)
3. "Это достаточно важно, чтобы отвлечь пользователя?" (дешёвый вызов)

**Зачем:** Агент не спамит рутиной, уведомляет только когда реально важно.

**Конфиг:**
```yaml
heartbeat:
  enabled: true
  interval_minutes: 30
  file: "HEARTBEAT.md"
  evaluate_before_notify: true
```

**Статус:** [x] готово — `src/heartbeat.py`, 3-фазный LLM evaluator, интеграция с MessageBus

---

### 2.4 Subagent Pattern + Orchestrator — изолированные агенты
**Что:** Каждый агент в fleet = изолированный runner:
- Свой ToolRegistry (coder → shell + filesystem, analyst → web_search + read_file)
- Свой system prompt из SOUL.md + skills
- Максимум 15 итераций (защита от бесконечных циклов)
- Результат возвращается оркестратору через MessageBus

**Orchestrator:** `src/orchestrator.py` — маршрутизация сообщений между агентами:
- Прямое обращение: target="agent:{name}"
- По chat_id (каждый бот = свой chat)
- Single-agent passthrough (обратная совместимость)

**Статус:** [x] orchestrator готов — `src/orchestrator.py`, интеграция в `main.py`. Subagent isolation — backlog

---

### 2.5 Skills с проверкой зависимостей
**Что:** YAML frontmatter в скиллах с requirements:
```yaml
---
description: "Крипто-анализ"
requirements:
  commands: ["curl"]
  env: ["COINGECKO_API_KEY"]
always: false
---
```
Если зависимости не установлены — скилл не загружается.

**Статус:** [x] готово — YAML frontmatter в `agent.py` (parse_skill_frontmatter, check_skill_requirements), обновлены скиллы

---

### 2.6 Channel Plugin Registry
**Что:** Auto-discovery каналов через pkgutil + entry_points.
BaseChannel: start(), stop(), send(), is_allowed(), transcribe_audio().
Подготовка к Discord, WhatsApp, Slack.

**Статус:** [ ] не начато

---

### 2.7 Agent Manager — создание агентов через бота и CLI

**Что:** Сейчас добавление нового агента — полностью ручной процесс (создать папку, написать YAML, добавить токен в .env, перезапустить). Нужна автоматизация.

**Фаза 1 — CLI-визард** (`src/cli.py`):
```bash
python -m src.cli create-agent    # Интерактивное создание
python -m src.cli list-agents     # Список агентов и статусы
python -m src.cli validate        # Проверка конфигов
```
- Спрашивает: имя, display_name, токен от BotFather, описание роли, модель
- Генерирует: `agents/{name}/agent.yaml`, шаблонный `SOUL.md`, `memory/`, `skills/`
- Добавляет токен в `.env`
- Быстро реализовать, сразу полезно

**Фаза 2 — Telegram-команды** (в главном боте):
```
/create_agent    — диалоговое создание через Claude
/agents          — список всех агентов + статус (running/stopped)
/edit_agent hr   — изменить SOUL, модель, скиллы
/stop_agent hr   — остановить агента
/start_agent hr  — запустить агента
```
- Claude сам генерирует SOUL.md на основе описания пользователя
- Требует hot-reload в `main.py` (запуск нового бота без рестарта всего сервера)

**Фаза 3 — Продвинутое управление:**
- `/clone_agent` — скопировать агента как шаблон
- Библиотека шаблонов (маркетолог, разработчик, аналитик, HR...)
- Управление скиллами: добавить/удалить скилл у агента

**Ключевой новый модуль — `src/agent_manager.py`:**
```python
class AgentManager:
    create_agent(name, display_name, bot_token, description) -> Path
    list_agents() -> list[dict]          # name, status, memory_size
    validate_agent(agent_dir) -> (bool, list[str])
    delete_agent(name, purge_memory=False) -> bool
    hot_reload_agent(name) -> None       # Запуск без рестарта сервера
```

**Hot-reload в main.py:**
- Новый обработчик сигнала или bus-сообщение "reload_agent"
- Создаёт новый Agent + AgentWorker + TelegramBridge для нового агента
- Добавляет в общий asyncio.gather без остановки существующих

**Зачем:** Фаундер не программист — ему нужно создавать агентов так же легко, как создавать чат в Telegram. Ручная правка YAML и .env — барьер.

**Статус:** [x] готово — `src/agent_manager.py`, `src/cli.py`, Telegram-команды (/agents, /create_agent, /start_agent, /stop_agent), hot-reload в `FleetRuntime`

---

## Phase 3: Групповые чаты — Dual-mode (DM + Group)

### Концепция
Один бот работает в двух режимах, в зависимости от `chat.type`:
- **Private** — текущая логика, полный доступ, все настройки
- **Group/Supergroup** — изолированный режим, отвечает только по тегу/@mention или reply

### 3.1 Фильтр ответов в группах
**Что:** Бот молча читает ВСЕ сообщения в группе, но отвечает ТОЛЬКО когда:
1. Текст содержит `@botusername` (mention)
2. Сообщение — reply на сообщение бота
3. Команда `/` адресована боту (`/help@botname`)

**Где:** `telegram_bridge.py` — добавить `_should_respond(update) -> bool`

**Как:**
```python
def _should_respond(self, update: Update) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return True  # В DM отвечаем всегда
    
    msg = update.message
    # Упомянут @botname в тексте?
    if self._bot_username and f"@{self._bot_username}" in (msg.text or ""):
        return True
    # Reply на сообщение бота?
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.id == self._bot_id:
            return True
    return False
```

**Статус:** [ ] не начато

---

### 3.2 Тихое логирование группы
**Что:** Все сообщения группы (включая те, на которые бот не отвечает)
записываются в `memory/groups/{chat_id}/daily/` для контекста.

**Где:** `memory.py` — новые функции `log_group_message()`, `read_group_context()`

**Структура памяти:**
```
memory/
├── profile.md              # Личный (только для DM)
├── wiki/                   # Личная wiki
├── daily/                  # Личные логи
├── groups/
│   ├── -100123456789/      # Чат "Команда Product"
│   │   ├── context.md      # О чём чат, участники, правила
│   │   ├── daily/          # Логи группового чата
│   │   └── wiki/           # Групповые знания
│   └── -100987654321/      # Другой чат
│       └── ...
```

**Формат daily note в группе:**
```markdown
# 2026-04-11 Friday — Команда Product

**09:15** 👤 Алексей: Релиз готов к деплою
**09:16** 👤 Марина: Тесты ещё не прошли, подожди
**09:30** 👤 Алексей: @bot что думаешь о рисках деплоя сейчас?
**09:31** 🤖 Бот: Марина права — подождите тесты. Риски: ...
```

Каждое сообщение логируется с именем автора (не только user/assistant).

**Статус:** [ ] не начато

---

### 3.3 Изолированный system prompt для групп
**Что:** В группе бот НЕ видит личный profile.md. Вместо этого:
- Читает `groups/{chat_id}/context.md` (описание группы)
- Читает `groups/{chat_id}/daily/` (сегодняшний лог группы)
- Читает `groups/{chat_id}/wiki/` (групповые знания)
- SOUL.md — общий, но с инструкцией "ты в групповом чате"

**Где:** `agent.py` — новый метод `build_group_system_prompt(chat_id)`

**Дополнительная инструкция в system prompt для группы:**
```
Ты в групповом чате. Правила:
- Отвечай только когда к тебе обращаются
- Учитывай контекст предыдущих сообщений (они в логе ниже)
- Обращайся к участникам по имени
- Не выдавай личную информацию пользователя-владельца
- Будь краток — в групповом чате никто не читает длинные ответы
```

**Статус:** [ ] не начато

---

### 3.4 Групповой онбординг
**Что:** При первом добавлении бота в группу:
1. Бот отправляет приветствие: "Привет! Я буду следить за контекстом беседы.
   Упомяните @me когда нужна помощь."
2. Создаёт `groups/{chat_id}/context.md` с шаблоном
3. Начинает молча логировать

**Триггер:** Событие `ChatMemberUpdated` (бот добавлен) ИЛИ первое сообщение
в новой группе.

**Шаблон context.md:**
```markdown
# Группа: [название чата]
- Chat ID: -100...
- Тип: group/supergroup
- Добавлен: 2026-04-11
- Участники: [заполнится автоматически]
- Тема: [заполни или бот определит сам]
```

**Статус:** [ ] не начато

---

### 3.5 Команды в группах
**Что:** Команды в группах работают так же, но:
- `/model`, `/restore`, `/memory` — только для owner (FOUNDER_TELEGRAM_ID)
- `/help`, `/status` — для всех участников
- Команда без `@botname` может быть проигнорирована (если в группе несколько ботов)

**Где:** Обновить `_check_auth()` и `_handle_command()` для group permissions.

**Статус:** [ ] не начато

---

### 3.6 Порядок реализации Phase 3

1. **Фильтр ответов** (3.1) — самое важное, без него бот будет отвечать на всё в группе
2. **Тихое логирование** (3.2) — memory/groups/ структура
3. **Изолированный prompt** (3.3) — разделение контекста DM/group
4. **Групповой онбординг** (3.4) — UX при добавлении в чат
5. **Права команд** (3.5) — owner vs участники

---

### 2.8 Улучшения ядра из nanobot (новый спринт)

#### 2.8.1 Consolidator — умное сжатие контекста
**Что:** Когда разговор длинный, автоматически суммаризировать старые сообщения вместо
грубого обрезания. Решает проблему переполнения контекстного окна.

**Как работает (по примеру nanobot):**
1. Мониторить размер промпта (токены) перед каждым вызовом Claude
2. Когда приближаемся к лимиту — найти безопасную границу для сжатия (на стыке user-turn)
3. Вызвать дешёвую модель (haiku) для суммаризации старых сообщений
4. Заменить старые сообщения на суммаризацию
5. Если суммаризация не удалась — raw archiving (просто удалить старые)

**Где:**
- Новый файл `src/consolidator.py` (~250 строк)
- Интеграция в `agent.py` перед `call_claude()`
- Добавить в `agent.yaml`:
```yaml
consolidator:
  enabled: true
  max_context_ratio: 0.8    # сжимать при 80% заполнения
  summary_model: "haiku"
  min_messages_to_keep: 5   # последние 5 сообщений не трогать
```

**Зачем:** Сейчас длинные разговоры упираются в контекстное окно. Consolidator
позволит вести беседу часами без потери контекста.

**Статус:** [ ] не начато

---

#### 2.8.2 Hook-система — lifecycle hooks для расширяемости
**Что:** Точки расширения в жизненном цикле агента без модификации ядра.

**Хуки:**
- `before_call` — перед вызовом Claude (можно модифицировать промпт)
- `on_stream` — при получении streaming-чанка (логирование, метрики)
- `after_call` — после завершения вызова (аналитика, постобработка)
- `on_tool_use` — при использовании инструмента (аудит, ограничения)
- `on_error` — при ошибке (алертинг, fallback)

**Архитектура:**
```python
@dataclass
class Hook:
    name: str
    async def execute(self, context: dict) -> dict: ...

class CompositeHook(Hook):
    """Вызывает несколько хуков с error isolation — сломанный хук не роняет цикл"""
    hooks: list[Hook]

class HookRegistry:
    register(event: str, hook: Hook)
    emit(event: str, context: dict) -> dict
```

**Где:** Новый файл `src/hooks.py` (~100 строк), интеграция в `agent.py`

**Зачем:** Позволяет добавлять кастомное поведение (метрики, фильтрацию, аудит)
без правки основного кода агента.

**Статус:** [ ] не начато

---

#### 2.8.3 Блокировка опасных команд
**Что:** Паттерн-матчинг для предотвращения выполнения деструктивных команд через Bash tool.

**Блокируемые паттерны:**
```python
DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",           # rm -rf /
    r"rm\s+-rf\s+~",           # rm -rf ~
    r"mkfs\.",                  # форматирование диска
    r"dd\s+if=.*of=/dev/",     # перезапись диска
    r":()\{.*\|.*&",           # fork bomb
    r"DROP\s+(TABLE|DATABASE)", # SQL деструктивные
    r"TRUNCATE\s+TABLE",       # SQL очистка таблиц
    r"DELETE\s+FROM\s+\w+\s*;",# DELETE без WHERE
    r"git\s+push\s+.*--force", # force push
    r"chmod\s+-R\s+777\s+/",   # открытие прав на всё
    r">\s*/dev/sd",            # перезапись блочного устройства
]
```

**Поведение:** При обнаружении — отклонить команду и уведомить пользователя:
"⚠️ Заблокирована потенциально опасная команда: `rm -rf /`. Используй /allow для однократного разрешения."

**Где:**
- Новый файл `src/command_guard.py` (~80 строк)
- Интеграция через Hook-систему (`on_tool_use` хук) или напрямую в `agent.py`

**Зачем:** Claude иногда генерирует деструктивные команды по ошибке.
Паттерн-матчинг — простая и надёжная защита.

**Статус:** [ ] не начато

---

#### 2.8 Порядок реализации

1. **Hook-система** (2.8.2) — фундамент, от неё зависят остальные
2. **Блокировка опасных команд** (2.8.3) — первый хук, сразу полезен
3. **Consolidator** (2.8.1) — самый сложный, но самый ценный

---

## Phase 4: Умные агенты — эффективность и проактивность

> Промпт-файлы для каждой задачи лежат в корне: `PROMPT_*.md`
> Порядок: 4.1 → 4.4 → 4.2 → 4.3 (по убыванию impact / effort)

### 4.1 Семантический поиск по памяти
**Промпт:** `PROMPT_SEMANTIC_SEARCH.md`

**Что:** Агент сейчас видит только profile + index + сегодняшний лог. Wiki-страницы двухнедельной давности — недоступны. Нужен поиск по wiki при каждом запросе.

**Решение:**
- Функция `search_wiki()` в memory.py — текстовый поиск по ключевым словам
- Токенизация запроса, стоп-слова (ru + en)
- Scoring: совпадение в заголовке x3, в имени файла x2
- Топ-3 релевантные страницы подкладываются в system prompt
- Лимит: 4000 символов суммарно

**Где:** `src/memory.py` → `search_wiki()`, обновить `read_context()` и `agent.py`

**Статус:** [ ] не начато

---

### 4.2 Делегация между агентами
**Промпт:** `PROMPT_AGENT_DELEGATION.md`

**Что:** Агенты изолированы. Стратег ("me") не может попросить кодера ("coder") написать код. Bus поддерживает A2A тип, но нигде не используется.

**Решение:**
- Агент записывает `delegation/{agent_name}.task.md` через Write
- `DelegationManager` мониторит файлы, передаёт через bus, ждёт ответ
- Ответ в `delegation/{agent_name}.result.md` — агент читает через Read
- В system prompt: секция "Другие агенты в команде" с описаниями

**Где:** Новый `src/delegation.py`, обновить `agent.py`, `agent_worker.py`, `main.py`

**Статус:** [x] готово — `src/delegation.py`, master/worker иерархия (role в agent.yaml), 3-уровневая защита (prompt, infrastructure, bus)

---

### 4.3 Умный Heartbeat с триггерами
**Промпт:** `PROMPT_SMART_HEARTBEAT.md`

**Что:** Heartbeat — тупой таймер. Нужны умные триггеры: утренний брифинг, вечерний дайджест, проверка дедлайнов, мониторинг новостей.

**Решение:**
- Конфиг `triggers:` в agent.yaml — cron + prompt + model + notify
- `SmartHeartbeat` класс — проверяет триггеры каждую минуту
- Cron-парсинг из существующего `src/cron.py`
- notify: true (всегда), false (никогда), "auto" (LLM решает)

**Где:** Новый `src/smart_heartbeat.py`, обновить `main.py`, `agents/me/agent.yaml`

**Статус:** [ ] не начато

---

### 4.4 Умное управление контекстом
**Промпт:** `PROMPT_SMART_CONTEXT.md`

**Что:** System prompt наполняется статично. Index.md растёт, daily обрезается грубо. Нет приоритизации.

**Решение:**
- Context Budget — бюджет символов для каждой секции
- Hot Pages — трекинг частоты обращений к wiki-страницам
- Smart Daily — свежие сообщения полностью, ранние — summary
- Приоритет: profile > hot pages > wiki search > daily > index

**Где:** `src/memory.py` → `build_smart_context()`, обновить `agent.py`

**Статус:** [ ] не начато

---

### 4.5 MCP-серверы — реальные интеграции
**Что:** В agent.yaml уже есть поддержка `mcp_servers`, но ничего не подключено.

**Приоритетные интеграции:**
- Google Calendar — расписание, встречи
- GitHub — PR, issues, code review
- Todoist/Linear — задачи, проекты
- Slack — мониторинг каналов

**Как подключить:** Добавить в agent.yaml:
```yaml
mcp_servers:
  calendar:
    command: "npx"
    args: ["-y", "@anthropic/mcp-google-calendar"]
    env:
      GOOGLE_CALENDAR_CREDENTIALS: "${GOOGLE_CALENDAR_CREDENTIALS}"
```

**Статус:** [ ] не начато

---

### 4.6 Голосовые ответы (TTS)
**Что:** Голос работает только на вход (Deepgram STT). Нет обратной связи голосом.

**Решение:**
- При коротких ответах (<500 символов) — генерировать OGG через TTS API
- Отправлять как voice message в Telegram
- Настройка в settings.json: text/voice/both

**Статус:** [ ] не начато

---

### 4.7 Аналитика и метрики
**Что:** Нет метрик: сколько запросов, latency, популярные темы, стоимость.

**Решение:**
- Команда `/stats` — запросов за день/неделю, средняя latency, топ-темы
- Логирование в `memory/stats/usage.jsonl`
- Данные уже есть в `raw/conversations/` — нужно считать

**Статус:** [ ] не начато

---

### 4.8 Шаблоны агентов
**Что:** При создании агента — один шаблон на всех.

**Решение:**
- Библиотека в `templates/agents/` — researcher, coder, content, support
- Каждый шаблон: свой SOUL.md, набор tools, skills
- При `/create_agent` — выбор шаблона

**Статус:** [ ] не начато

---

### 4.9 Оценка качества ответов (LLM-as-Judge)
**Что:** Нет способа понять, хорошо ли отвечает агент.

**Решение:**
- После ответа — haiku оценивает: релевантность, полнота, галлюцинации
- Плохие ответы → флаг в лог
- Статистика по `/stats`

**Статус:** [ ] не начато

---

### 4.10 Мультиканальность (Discord, Slack, Web)
**Что:** Архитектура с FleetBus готова — нужны новые Bridge-классы.

**Решение:**
- `src/discord_bridge.py` — Discord бот
- `src/web_bridge.py` — простой веб-чат
- Общий интерфейс: `BaseBridge` с методами start(), stop(), send()

**Статус:** [ ] не начато

---

### Phase 4: порядок реализации

| # | Задача | Impact | Effort | Промпт |
|---|--------|--------|--------|--------|
| 1 | Семантический поиск | 🔴 высокий | 🟢 низкий | `PROMPT_SEMANTIC_SEARCH.md` |
| 2 | Умный контекст | 🔴 высокий | 🟡 средний | `PROMPT_SMART_CONTEXT.md` |
| 3 | Делегация агентов | 🔴 высокий | 🟡 средний | `PROMPT_AGENT_DELEGATION.md` |
| 4 | Умный Heartbeat | 🟡 средний | 🟡 средний | `PROMPT_SMART_HEARTBEAT.md` |
| 5 | MCP-серверы | 🟡 средний | 🟢 низкий | конфиг в agent.yaml |
| 6 | TTS ответы | 🟡 средний | 🟢 низкий | — |
| 7 | Аналитика | 🟢 низкий | 🟢 низкий | — |
| 8 | Шаблоны агентов | 🟢 низкий | 🟢 низкий | — |
| 9 | LLM-as-Judge | 🟢 низкий | 🟡 средний | — |
| 10 | Мультиканальность | 🟡 средний | 🔴 высокий | — |

---

## Другие идеи из nanobot (backlog)

- **Sandbox через bubblewrap** — дополнительный слой безопасности для exec
- **Session-scoped concurrency** — параллельные сессии, но сериализация внутри одной
- **Evaluator chain** — цепочка LLM-вызовов для принятия решений
- **Абстракция провайдеров** — базовый LLMProvider для поддержки разных моделей (когда понадобится)
- **Checkpoint recovery** — восстановление после прерванных вызовов LLM
