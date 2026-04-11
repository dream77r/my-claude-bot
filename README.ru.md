# My Claude Bot

[🇬🇧 English version](README.md)

Multi-agent Telegram-платформа на базе Claude Agent SDK. Флот AI-агентов с общей шиной сообщений, фоновой обработкой памяти, cron-задачами и MCP-интеграциями. Работает через Claude Pro-подписку ($20/мес, безлимит), не через API.

## Что умеет

- **Multi-agent fleet** -- несколько агентов, каждый со своим Telegram-ботом, SOUL и скиллами
- **MessageBus** -- асинхронная шина сообщений между агентами (pub/sub, broadcast, prefix routing)
- **Streaming ответов** -- текст появляется в Telegram по мере генерации, не одним блоком
- **Dream Memory** -- фоновая 2-фазная обработка памяти по расписанию (извлечение фактов + обновление wiki)
- **Heartbeat** -- проактивный агент: проверяет задачи, выполняет, решает стоит ли уведомлять
- **Cron-задачи** -- периодические задачи с cron-выражениями (сводки, дайджесты, мониторинг)
- **MCP-серверы** -- подключение Todoist, GitHub, Google Calendar и любых MCP через конфиг
- **Wiki-память** (модель Karpathy) -- автоматически записывает людей, решения, идеи с git-версионированием
- **Skills с зависимостями** -- YAML frontmatter: проверка команд и env-переменных перед загрузкой
- **Голосовые сообщения** -- транскрипция через Deepgram API (Nova-3)
- **Файлы** -- приём и анализ документов, фото через Telegram (до 20MB)
- **Групповые чаты** -- dual-mode (DM + группы), тихое логирование, изолированная память, поддержка топиков
- **Онбординг** -- выбор языка + заполнение профиля при первом запуске
- **i18n** -- английский и русский интерфейс, язык сохраняется для каждого пользователя

## Быстрый старт

```bash
git clone https://github.com/dream77r/my-claude-bot.git && cd my-claude-bot && ./setup.sh
```

Одна команда. Скрипт проверит Docker и Claude CLI, спросит токен бота и Telegram ID, соберёт и запустит всё. Открой Telegram и напиши боту — он проведёт онбординг.

**Требования:** Docker, Claude CLI (установлен и авторизован), Claude Pro подписка.

## Ручная настройка Docker

```bash
cp .env.example .env
# заполни .env: HOST_HOME, токен бота, Telegram ID

docker compose up -d --build
```

Бот автоматически:
- перезапускается при падении
- стартует при перезагрузке сервера
- ограничен по памяти (1GB) и CPU (2 ядра)

## Голосовые сообщения

Бот распознаёт голосовые через Deepgram API (модель Nova-3). Стоимость ~$0.0043/мин, есть бесплатный тариф на $200.

**Настройка (два способа):**

1. **Через чат** -- отправь боту: "Вот ключ Deepgram: твой_ключ"
2. **Через `.env`** -- добавь `DEEPGRAM_API_KEY=твой_ключ`

Получить ключ: https://console.deepgram.com/

**Как работает:** Telegram голосовое (OGG) → скачивание → Deepgram API → текст → обработка как обычное сообщение.

## Групповые чаты

Бот работает в двух режимах в зависимости от `chat.type`:
- **Private** -- полный доступ, все настройки, личная память
- **Group/Supergroup** -- изолированный режим, отвечает только по @mention или reply

**Возможности групп:**
- Тихое логирование ВСЕХ сообщений (накапливает контекст не отвечая)
- Изолированная память для каждой группы (`memory/groups/{chat_id}/`)
- Отдельный system prompt (нет доступа к личным данным владельца)
- Поддержка топиков/тем (отвечает в правильном топике, можно ограничить одним)
- Настройка из DM: при добавлении в группу бот пишет владельцу и спрашивает как себя вести
- Разграничение команд: только для владельца (`/model`, `/restore`) и для всех (`/help`, `/status`)

## MCP-серверы (Todoist, GitHub и другие)

Claude CLI поддерживает MCP (Model Context Protocol) серверы -- внешние инструменты, которые агент может использовать напрямую.

**Настройка:** раскомментируй секцию `mcp_servers` в `agents/me/agent.yaml`:

```yaml
mcp_servers:
  todoist:
    command: "npx"
    args: ["-y", "@anthropic/mcp-todoist"]
    env:
      TODOIST_API_TOKEN: "${TODOIST_API_TOKEN}"
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_PERSONAL_ACCESS_TOKEN}"
```

Добавь токены в `.env`:
```bash
TODOIST_API_TOKEN=your_todoist_token
GITHUB_PERSONAL_ACCESS_TOKEN=your_github_token
```

Где получить токены:
- Todoist: https://todoist.com/app/settings/integrations/developer
- GitHub: https://github.com/settings/tokens (scopes: repo, read:org)

Можно подключить любой MCP-сервер -- формат совпадает с конфигом Claude CLI.

## Cron-задачи

Периодические задачи с cron-выражениями. Агент выполняет промпт по расписанию и уведомляет в Telegram.

**Конфиг в `agent.yaml`:**

```yaml
cron:
  - name: "daily_digest"
    schedule: "0 21 * * *"          # каждый день в 21:00
    prompt: "Сделай резюме дня на основе daily note"
    model: "haiku"
    notify: true
  - name: "weekly_summary"
    schedule: "0 9 * * 1"           # каждый понедельник в 9:00
    prompt: "Сделай сводку за неделю: ключевые решения, прогресс, блокеры"
    model: "sonnet"
    notify: true
```

Поддерживаемые cron-выражения: `*`, `*/N`, `N-M`, `N,M,K`, точные значения. Формат: `минуты часы день месяц день_недели`.

## Требования

- Python 3.10+ или Docker
- Claude CLI (установлен и авторизован)
- Claude Pro подписка
- Telegram-бот (создать у @BotFather)
- Deepgram API ключ (опционально, для голосовых)
- Node.js/npx (опционально, для MCP-серверов)

## Архитектура

```
Telegram User → TelegramBridge → MessageBus → AgentWorker → Agent.call_claude()
                     ↑                                            ↓
                bus listener ← ── ── ── ── ── ── ← ── ── ── response/streaming
                     ↓
              StatusMessage (streaming, tool hints)

Фоновые процессы:
  Dream loop (каждые N часов) → Phase 1 (haiku: извлечение фактов)
                               → Phase 2 (sonnet: обновление wiki)
  Heartbeat (каждые 30 мин)  → Check → Execute → Evaluate → Notify?
  Cron (по расписанию)       → Execute prompt → Notify
```

## Структура проекта

```
src/
  main.py             -- точка входа, запуск fleet
  agent.py            -- Agent: конфиг, system prompt, вызов Claude, MCP
  agent_worker.py     -- AgentWorker: связка Agent с MessageBus
  telegram_bridge.py  -- Telegram-хэндлеры, message aggregation, streaming
  bus.py              -- MessageBus: pub/sub шина на asyncio.Queue
  orchestrator.py     -- маршрутизация между агентами
  dream.py            -- Dream Memory: фоновая обработка памяти
  heartbeat.py        -- Heartbeat: проактивные задачи
  cron.py             -- Cron: периодические задачи по расписанию
  memory.py           -- Karpathy Wiki: profile, wiki/, daily notes, git-backed
  command_router.py   -- 4-уровневый роутер команд
  agent_manager.py    -- Agent Manager: создание/список/валидация агентов
  cli.py              -- CLI-интерфейс для управления агентами
  tool_hints.py       -- статусы инструментов
  voice_handler.py    -- голосовые через Deepgram API
  file_handler.py     -- загрузка/отправка файлов

agents/me/                    -- стратегический советник
  agent.yaml                  -- конфиг (bot_token, skills, dream, heartbeat, cron, mcp)
  SOUL.md                     -- личность агента
  skills/                     -- скиллы с YAML frontmatter
  templates/                  -- промпт-шаблоны для Dream
  memory/                     -- хранилище с git-версионированием

agents/coder/                 -- технический агент
  agent.yaml                  -- конфиг (Bash, Edit, Grep и другие dev-tools)
  SOUL.md                     -- личность кодера
  skills/                     -- code-review, debugging
  templates/                  -- промпт-шаблоны для Dream
  memory/                     -- хранилище с git-версионированием

agents/team/                  -- командный ассистент (групповой чат)
  agent.yaml                  -- конфиг (групповой доступ, research, task-tracking)
  SOUL.md                     -- личность Team Hub
  skills/                     -- task-tracking, knowledge-base, research
  templates/                  -- промпт-шаблоны для Dream
  memory/                     -- хранилище с git-версионированием
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие + онбординг при первом запуске |
| `/help` | Справка по командам |
| `/newsession` | Сбросить контекст (новая сессия Claude) |
| `/stop` | Остановить текущий запрос (работает всегда, даже при занятом агенте) |
| `/status` | Статус агента, сессии, последний бэкап памяти |
| `/memory` | История изменений памяти (git log) |
| `/restore` | Откатить память к предыдущей версии |
| `/dream` | Запустить Dream-обработку памяти вручную |
| `/model` | Сменить модель Claude (Haiku/Sonnet/Opus) |
| `/agents` | Список всех агентов и их статус |
| `/create_agent` | Создать нового агента через визард |
| `/start_agent` | Запустить агента по имени |
| `/stop_agent` | Остановить агента по имени |

## Конфиг агента (agent.yaml)

```yaml
name: "me"
display_name: "Стратегический советник"
bot_token: "${ME_BOT_TOKEN}"
system_prompt: |
  Ты — персональный стратегический советник...

skills:
  - "document-analysis"
  - "web-research"
allowed_users:
  - ${FOUNDER_TELEGRAM_ID}
claude_model: "sonnet"
claude_flags:
  - "--allowedTools"
  - "Read,Write,Glob,Grep,WebSearch,WebFetch"

# MCP-серверы (опционально)
mcp_servers:
  todoist:
    command: "npx"
    args: ["-y", "@anthropic/mcp-todoist"]
    env:
      TODOIST_API_TOKEN: "${TODOIST_API_TOKEN}"

# Dream Memory — фоновая обработка памяти
dream:
  interval_hours: 2
  model_phase1: "haiku"
  model_phase2: "sonnet"

# Heartbeat — проактивные задачи
heartbeat:
  enabled: true
  interval_minutes: 30

# Cron — периодические задачи
cron:
  - name: "daily_digest"
    schedule: "0 21 * * *"
    prompt: "Сделай резюме дня"
    model: "haiku"
    notify: true
```

## Добавление нового агента

### Через Telegram (рекомендуется)

Отправь `/create_agent` своему главному боту. Визард проведёт через 5 шагов:

1. Имя агента (латиницей, для папки)
2. Отображаемое имя
3. Токен бота (создай нового бота у @BotFather)
4. Описание роли
5. Модель Claude (haiku / sonnet / opus)

Агент запускается сразу через hot-reload -- перезапуск не нужен.

### Через CLI

```bash
python -m src.cli create-agent    # интерактивный визард
python -m src.cli list-agents     # список всех агентов
python -m src.cli validate        # проверка конфигов
```

### Управление агентами

| Команда | Описание |
|---------|----------|
| `/agents` | Список всех агентов со статусом (запущен/остановлен/нет токена) |
| `/create_agent` | Создать нового агента через Telegram-визард |
| `/start_agent имя` | Запустить агента |
| `/stop_agent имя` | Остановить агента |

Каждый агент -- отдельный Telegram-бот с изолированной памятью. Orchestrator автоматически маршрутизирует сообщения через MessageBus.

## Docker-команды

```bash
docker compose ps          # статус
docker compose logs -f     # логи в реальном времени
docker compose restart     # перезапуск
docker compose down        # остановка
docker compose up -d --build  # пересобрать и запустить
```

## Безопасность

- Токены хранятся в `.env` (не попадает в git)
- Доступ к боту только для указанных Telegram ID (`allowed_users`)
- Claude CLI работает с ограниченным набором инструментов (`allowedTools`)
- Docker-контейнер изолирован от основной системы
- Память каждого агента изолирована (свой `memory/`)
- Git-версионирование памяти с возможностью отката (`/restore`)
- Групповые чаты: личные данные владельца никогда не попадают в system prompt группы

## Roadmap

- **Phase 1 (завершена):** персональный ассистент, файлы, голосовые, память, онбординг, git-backed wiki
- **Phase 2 (завершена):** MessageBus, Orchestrator, Dream Memory, Heartbeat, Skills frontmatter
- **Phase 3 (завершена):** multi-agent fleet, групповые чаты, топики, streaming, MCP, cron, i18n
