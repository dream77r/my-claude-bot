# My Claude Bot

[🇬🇧 English version](README.md)

**Что это в 5 предложениях.** My Claude Bot превращает твою подписку Claude Pro ($20/мес) или Claude Max ($100/мес, $200/мес) во флот AI-агентов в Telegram — стратегический советник, кодер, командный хаб и документный архивариус из коробки, плюс неограниченно своих агентов, каждый как отдельный Telegram-бот со своей личностью и памятью. Агенты общаются через MessageBus, помнят всё в wiki-памяти с git-версионированием и самообучаются по ночам через Dream-циклы, которые анализируют паттерны использования и предлагают новые скиллы или улучшения схемы. Работает в личных чатах и группах, принимает файлы и голосовые, ставит скиллы из общего маркетплейса сообщества и крутится без присмотра на любом дешёвом VPS. Никаких API-счетов, никакой оплаты за вызовы — одна команда поставить, одна команда обновить. Если хочешь постоянную личную AI-команду, которая живёт прямо в Telegram, где ты и так сидишь, и стоит фиксированно в месяц без сюрпризов — это для тебя.

```
         Telegram  (личные чаты + группы + форум-топики)
       ┌──────┬────────┬───────┬────────────┬──────────┐
       │      │        │       │            │          │
       ▼      ▼        ▼       ▼            ▼          ▼
      me    coder    team  archivist     custom…    custom…
  (советник)(код) (группа) (документы)   (твои агенты)
       │      │        │       │            │          │
       └──────┴────────┴───────┴────────────┴──────────┘
                             │
                   MessageBus + Orchestrator
                    (делегирование, роутинг)
                             │
           ┌─────────────────┼──────────────────┐
           │                 │                  │
     ┌─────▼──────┐   ┌──────▼──────┐   ┌───────▼───────┐
     │ Claude Pro │   │ Wiki-память │   │   Скиллы +    │
     │  или Max   │   │ у каждого,  │   │ общий Pool-   │
     │ (фикс/мес) │   │ git-backed  │   │  маркетплейс  │
     └────────────┘   └─────────────┘   └───────────────┘

   Фоновое: Dream (4 фазы) · Knowledge Graph · Smart Heartbeat · Cron
            SkillAdvisor / SchemaAdvisor · Consolidator · Sandbox
```

## Что умеет

- **Четыре базовых агента** -- `me` (стратегический советник / master), `coder` (разработка), `team` (групповой хаб), `archivist` (domain-agnostic документный архив)
- **Multi-agent fleet** -- неограниченно добавляй своих агентов, каждый со своим Telegram-ботом, SOUL и скиллами
- **Делегирование агентов** -- иерархия master/worker, Orchestrator маршрутизирует сообщения через MessageBus
- **Sandbox** -- изоляция файловой системы для worker-агентов, индивидуальный набор разрешённых инструментов
- **MessageBus** -- асинхронная шина сообщений между агентами (pub/sub, broadcast, prefix routing)
- **Streaming ответов** -- текст появляется в Telegram по мере генерации, не одним блоком
- **Dream Memory** -- фоновая 3+ фазная обработка памяти (извлечение фактов, обновление wiki, анализ паттернов)
- **Knowledge Graph** -- 3-уровневый ночной пайплайн линковки памяти (Obsidian-style `[[links]]`, дневные саммари, синтез графа)
- **SkillAdvisor / SchemaAdvisor** -- агенты анализируют паттерны использования и проактивно предлагают новые скиллы или улучшения схемы, никогда не применяют автоматически
- **Skill Pool маркетплейс** -- устанавливай скиллы из общего пула сообщества (`/poolskills`, `/installskill`), hot-reload без перезапуска
- **SkillCreator** -- динамическое создание скиллов через оркестратор по запросу
- **Smart Heartbeat** -- проактивный агент с cron-триггерами: проверяет задачи, выполняет, решает стоит ли уведомлять
- **Smart Context Management** -- бюджетная система с семантическим поиском по wiki, держит контекст в пределах 200K
- **Cron-задачи** -- периодические задачи с cron-выражениями (сводки, дайджесты, мониторинг)
- **MCP-серверы** -- подключение Todoist, GitHub, Google Calendar и любых MCP через конфиг
- **Wiki-память** (модель Karpathy) -- автоматически записывает людей, решения, идеи с git-версионированием и откатами
- **Skills с frontmatter** -- соответствие спеке agentskills.io, progressive disclosure, multi-file bundles, проверка зависимостей
- **Hook-система, Command Guard, Consolidator** -- pre/post-hooks, политики allow/deny для команд, уплотнение памяти
- **Голосовые сообщения** -- транскрипция через Deepgram API (Nova-3)
- **Файлы** -- приём и анализ документов, фото через Telegram (до 20MB); outbox-паттерн для отправки файлов обратно
- **Групповые чаты** -- dual-mode (DM + группы), тихое логирование, изолированная память, поддержка топиков
- **Горячее управление агентами** -- `/create_agent`, `/clone_agent`, `/set_access`, `/start_agent`, `/stop_agent` без перезапуска сервиса
- **Онбординг** -- выбор языка + заполнение профиля при первом запуске, `/start` автоматически регистрирует первого клиента
- **i18n** -- английский и русский интерфейс, язык сохраняется для каждого пользователя

## Быстрый старт

```bash
git clone https://github.com/dream77r/my-claude-bot.git && cd my-claude-bot && ./setup.sh
```

Скрипт:
1. Проверит зависимости (Python, Claude CLI)
2. Спросит токен бота (с инструкцией как получить у @BotFather)
3. Спросит твой Telegram ID (с инструкцией как узнать через @userinfobot)
4. Спросит лимиты ресурсов (память, CPU) с учётом твоего сервера
5. Установит пакеты, создаст `.env`, настроит автозапуск
6. Запустит бота

Открой Telegram и напиши боту — дальше всё через чат (онбординг, язык, настройки).

**Требования:** Python 3.10+, Claude CLI (установлен и авторизован), подписка Claude Pro или Claude Max.

## Обновление

```bash
cd ~/my-claude-bot && git pull && ./update.sh
```

Одна команда: перейдёт в папку проекта, скачает последний код, обновит зависимости при необходимости и перезапустит сервис. Данные в безопасности -- `.env`, память агентов, `SOUL.md` и настройки не затрагиваются.

## Запуск через systemd (рекомендуется)

```bash
cp .env.example .env
# заполни .env: токен бота, Telegram ID

# Создать пользовательский systemd-сервис
mkdir -p ~/.config/systemd/user
cp my-claude-bot.service ~/.config/systemd/user/
# Отредактируй сервис: укажи пути WorkingDirectory и Environment

systemctl --user daemon-reload
systemctl --user enable my-claude-bot   # автозапуск при перезагрузке
systemctl --user start my-claude-bot    # запустить сейчас
sudo loginctl enable-linger $USER       # работать после выхода из SSH
```

Бот автоматически:
- перезапускается при падении (через 5 сек)
- стартует при перезагрузке сервера
- ограничен по памяти (1 GB) и CPU (2 ядра)
- отправляет уведомление в Telegram при каждом (пере)запуске

**Полезные команды:**
```bash
systemctl --user status my-claude-bot    # статус
journalctl --user -u my-claude-bot -f    # логи в реальном времени
systemctl --user restart my-claude-bot   # ручной перезапуск
```

## Запуск через Docker (альтернатива)

```bash
cp .env.example .env
# заполни .env: HOST_HOME, токен бота, Telegram ID

docker compose up -d --build
```

Важно: Claude CLI должен быть доступен внутри контейнера (монтируется через volumes в `docker-compose.yml`).

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
- Подписка Claude Pro или Claude Max (любой тариф)
- Telegram-бот (создать у @BotFather)
- Deepgram API ключ (опционально, для голосовых)
- Node.js/npx (опционально, для MCP-серверов)

## Архитектура

```
Telegram User → TelegramBridge → MessageBus → AgentWorker → Agent.call_claude()
                     ↑                                            ↓
                bus listener ← ── ── ── ── ── ← ── ── ── ── response/streaming
                     ↓                                            ↓
              StatusMessage (streaming, tool hints)         Delegation → worker-агент
                                                            (sandboxed)

Фоновые процессы:
  Dream loop (каждые N часов)  → Phase 1  (haiku: извлечение фактов)
                                → Phase 2  (sonnet: обновление wiki)
                                → Phase 3  (SkillAdvisor: анализ паттернов → предложения скиллов)
                                → Phase 3b (SchemaAdvisor: анализ vault'а → предложения по схеме, только для archivist)
  Knowledge Graph (ночной)     → Level 1 (линковка дневных заметок через [[wiki]])
                                → Level 2 (дневные саммари с кросс-ссылками)
                                → Level 3 (синтез графа, адаптивное расписание)
  Smart Heartbeat (триггеры)   → Check → Execute → Evaluate → Notify?
  Cron (по расписанию)         → Execute prompt → Notify
  Consolidator                 → Уплотнение памяти при приближении к лимиту контекста
```

## Структура проекта

```
src/
  main.py               -- точка входа, запуск fleet
  agent.py              -- Agent: конфиг, system prompt, вызов Claude, MCP
  agent_worker.py       -- AgentWorker: связка Agent с MessageBus
  agent_manager.py      -- Agent Manager: создание/список/валидация агентов
  telegram_bridge.py    -- Telegram-хэндлеры, message aggregation, streaming
  bus.py                -- MessageBus: pub/sub шина на asyncio.Queue
  orchestrator.py       -- маршрутизация между агентами
  delegation.py         -- иерархия делегирования master/worker
  dispatcher.py         -- фоновая диспетчеризация сообщений с явной маршрутизацией по чатам
  dream.py              -- Dream Memory: 4-фазная фоновая обработка
  knowledge_graph.py    -- 3-уровневый ночной пайплайн линковки памяти
  skill_advisor.py      -- Dream Phase 3: анализ паттернов → предложения скиллов
  schema_advisor.py     -- Dream Phase 3b: анализ vault'а → предложения по схеме (archivist)
  skill_pool.py         -- маркетплейс скиллов сообщества (установка из общего пула)
  skill_creator.py      -- динамическое создание скиллов через оркестратор
  smart_heartbeat.py    -- проактивный агент с cron-триггерами
  heartbeat.py          -- legacy heartbeat (простой интервал)
  consolidator.py       -- уплотнение памяти при приближении к лимиту контекста
  hooks.py              -- pre/post-хуки исполнения
  command_guard.py      -- политики allow/deny для команд
  sandbox.py            -- изоляция файловой системы для worker-агентов
  cron.py               -- Cron: периодические задачи по расписанию
  memory.py             -- Karpathy Wiki: profile, wiki/, daily notes, git-backed
  input_sanitizer.py    -- валидация и санитизация ввода
  ssrf_protection.py    -- SSRF-защита для WebFetch/URL
  audit.py              -- журнал аудита security-операций
  metrics.py            -- сбор метрик
  checkpoint.py         -- чекпоинты сессий
  command_router.py     -- 4-уровневый роутер команд
  cli.py                -- CLI-интерфейс для управления агентами
  tool_hints.py         -- статусы инструментов
  voice_handler.py      -- голосовые через Deepgram API
  file_handler.py       -- загрузка/отправка файлов с outbox round-trip
  i18n.py               -- система локализации EN/RU

agents/me/                    -- стратегический советник (master)
agents/coder/                 -- технический агент (dev-tools: Bash, Edit, Grep)
agents/team/                  -- командный хаб (группы, task-tracking, research)
agents/archivist/             -- документный архив, domain-agnostic
  agent.yaml                  -- конфиг (5 скиллов, schema_advisor, cron vault-lint)
  skills/                     -- vault-init, document-ingest, archive-search,
                                 vault-lint, schema-evolve
  memory_template/            -- пустой публичный scaffold (CLAUDE.md, .vault-config.json)
  meta-templates/             -- шаблоны для генерации доменных шаблонов
  examples/                   -- референсные домены (small-business, ...)

В папке каждого агента лежат: agent.yaml, SOUL.md (gitignored, локальный),
skills/, templates/, memory/ (gitignored, сеется из memory_template/ при старте).
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие + онбординг при первом запуске, автоматически регистрирует первого клиента |
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
| `/clone_agent` | Скопировать SOUL, скиллы и настройки с существующего агента |
| `/start_agent` | Запустить агента по имени |
| `/stop_agent` | Остановить агента по имени |
| `/set_access` | Управлять доступом к агенту на лету (добавлять/удалять разрешённых пользователей) |
| `/poolskills` | Посмотреть скиллы в общем пуле сообщества |
| `/installskill` | Установить скилл из пула (hot-reload) |
| `/restart` | Перезапустить платформу (применяет обновления кода) |

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

## Безопасность

- Токены хранятся в `.env` (не попадает в git, права файла 600)
- Доступ к боту только для указанных Telegram ID (`allowed_users`)
- Claude CLI работает с ограниченным набором инструментов (`allowedTools`)
- Память каждого агента изолирована (свой `memory/`)
- Git-версионирование памяти с возможностью отката (`/restore`)
- Групповые чаты: личные данные владельца никогда не попадают в system prompt группы
- Ограничения ресурсов: память (1 GB), CPU (2 ядра), макс процессов (100)
- Уведомление в Telegram при каждом (пере)запуске
- Путь к Claude CLI определяется автоматически через `PATH` или переменную `CLAUDE_CLI_PATH`

## Мультипользовательский режим

На одном сервере можно запустить несколько независимых ботов:
- Каждый пользователь создаёт своего бота у @BotFather (уникальный токен)
- Каждый пользователь имеет свой `.env`, `agents/` и systemd-сервис
- Пользовательские systemd-сервисы полностью изолированы
- Конфликтов нет, пока токены ботов разные

## Roadmap

- **Phase 1 (завершена):** персональный ассистент, файлы, голосовые, память, онбординг, git-backed wiki
- **Phase 2 (завершена):** MessageBus, Orchestrator, Dream Memory, Heartbeat, Skills frontmatter, Consolidator, Hook-система, Command Guard
- **Phase 3 (завершена):** multi-agent fleet, групповые чаты, топики, streaming, MCP, cron, i18n
- **Phase 4 (завершена):** делегирование master/worker, семантический поиск по wiki, Smart Heartbeat с триггерами, Smart Context Management, Knowledge Graph (3-уровневая ночная линковка), SkillAdvisor (Dream Phase 3), SkillCreator
- **Phase 5 (завершена):** security hardening (sandbox, SSRF-защита, audit, санитизация ввода), метрики, полировка streaming, checkpoint, CI, `/set_access`, `/clone_agent`, file round-trip outbox, Skill Pool маркетплейс (установка из общего пула), агент Архивариус (4-й базовый) с SchemaAdvisor (Dream Phase 3b)
