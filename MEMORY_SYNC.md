# Шпаргалка: синхронизация памяти My Claude Bot

> Локальный файл, не уходит в GitHub (в .gitignore).
> Обновлять при изменении архитектуры памяти.

## Где живёт вся память

```
agents/{name}/memory/
├── profile.md                  ← кто пользователь (редко меняется)
├── index.md                    ← каталог wiki-страниц
├── log.md                      ← хронологический append-only лог
├── graph.json                  ← граф связей [from→to, strength, контекст]
│
├── daily/
│   ├── 2026-04-12.md          ← сегодняшний разговор (все реплики)
│   ├── 2026-04-11.md
│   └── summaries/              ← Уровень 2 Knowledge Graph
│       ├── 2026-04-12.md       ← итоги дня с [[ссылками]]
│       └── 2026-04-11.md
│
├── wiki/
│   ├── entities/               ← люди, компании, продукты
│   │   ├── ivan.md
│   │   └── acme-corp.md
│   ├── concepts/               ← идеи, решения
│   │   └── productx.md
│   └── synthesis/              ← выводы, паттерны
│       ├── recurring-themes.md      ← обновляется Уровнем 3
│       ├── decision-timeline.md     ← обновляется Уровнем 3
│       └── knowledge-clusters.md    ← обновляется Уровнем 3
│
├── raw/
│   ├── conversations/          ← JSONL бэкап (источник истины)
│   │   ├── conversations-2026-04-12.jsonl
│   │   └── conversations-2026-04-11.jsonl
│   └── files/                  ← загруженные документы
│
├── sessions/                   ← Claude CLI session IDs
│   ├── current_session_id
│   ├── .dream_cursor           ← позиция Dream
│   └── .synthesis_state.json   ← состояние Уровня 3 KG
│
└── stats/
    ├── page_hits.json          ← частота обращений к wiki
    └── metrics.jsonl           ← token usage, cost, latency
```

## Что когда запускается

| # | Процесс | Когда | Модель | Что читает | Что пишет |
|---|---------|-------|--------|------------|-----------|
| 0 | **log_message** | на каждое сообщение | — | — | `daily/YYYY-MM-DD.md` + `raw/conversations/*.jsonl` + `log.md` |
| 1 | **Dream Memory** | каждые **2 часа** (me) | haiku + sonnet | новые `conversations/` с курсора | `wiki/`, `profile.md`, `index.md` |
| 2 | **Morning briefing** | **09:00** ежедневно | sonnet | `wiki/concepts/`, вчерашний `daily` | Telegram уведомление |
| 3 | **Deadline check** | каждые **4 часа** | haiku | `wiki/` | Telegram (если есть что срочное) |
| 4 | **Evening digest** | **21:00** ежедневно | haiku | сегодняшний `daily` | Telegram уведомление |
| 5 | **Skill Advisor Digest** | **20:00** ежедневно | sonnet | `skill_suggestions/inbox/` | Telegram мастеру |
| 6 | **KG Level 1: Линковка** | **01:00 UTC / 04:00 MSK** | haiku | сегодняшний `daily/` | `daily/` + `graph.json` |
| 7 | **KG Level 2: Саммари** | сразу после L1 | haiku | `daily/` с [[связями]] | `daily/summaries/YYYY-MM-DD.md` |
| 8 | **KG Level 3: Синтез** | адаптивно (14 дней каждый день → потом каждые 3 дня) | haiku | все `daily/summaries/` + `wiki/synthesis/` | `wiki/synthesis/` + backlinks + `graph.json` |

## Как всё связывается между собой

```
          Сообщение в Telegram
                   │
                   ↓
          log_message (реал-тайм)
         ┌─────────┼──────────┐
         ↓         ↓          ↓
    daily/*.md  raw/*.jsonl  log.md
                   │
                   │  (курсор: .dream_cursor)
                   ↓
         ┌─ Dream Memory (каждые 2ч) ─┐
         │  Phase 1 (haiku): факты    │
         │  Phase 2 (sonnet+tools):   │
         │    → wiki/ + index + profile│
         │  Phase 3: skill patterns    │
         └────────────────────────────┘
                   │
                   ↓
              wiki/ (entities/concepts/synthesis)
                   │
                   │  (ночью 04:00 MSK)
                   ↓
  ┌─ Knowledge Graph Level 1 ──────────────┐
  │  Читает: сегодняшний daily              │
  │  LLM (haiku): сущности + связи          │
  │  Пишет: daily/ с [[Obsidian]] + graph.json│
  └────────────────┬────────────────────────┘
                   ↓
  ┌─ Knowledge Graph Level 2 ──────────────┐
  │  Читает: daily/ со [[связями]]           │
  │  LLM (haiku): темы, решения, action      │
  │  Пишет: daily/summaries/YYYY-MM-DD.md    │
  └────────────────┬────────────────────────┘
                   ↓
  ┌─ Knowledge Graph Level 3 (адаптивно) ──┐
  │  Читает: все daily/summaries/ +         │
  │          wiki/synthesis/ + graph.json   │
  │  LLM (haiku + tools): паттерны,         │
  │    эволюция, кластеры, backlinks        │
  │  Пишет: wiki/synthesis/ + graph.json +  │
  │    .synthesis_state.json                │
  └─────────────────────────────────────────┘
                   │
                   ↓
   Git commit всех изменений памяти
```

## Как пользователь видит связанную память

Когда приходит запрос "что там с Иваном?", агент строит контекст через `build_smart_context()`:

```
1. profile.md                          (2000 символов)
2. wiki_search("Иван")                 (2000 символов)
   ├─ прямое совпадение: wiki/entities/ivan.md
   └─ graph bonus: wiki/entities/acme-corp.md
                   (найдено через graph.json, strength ≥ 2)
3. hot pages                           (3000 символов)
4. daily recent (последние реплики)    (2000 символов)
5. daily summary (сжатое раннее)       (1000 символов)
6. index.md                            (1500 символов)
```

Итого до ~11.5K символов максимум приоритизированного контекста.

## Конфигурационные файлы

Вся настройка — в одном файле: `agents/me/agent.yaml`

### Dream Memory

```yaml
dream:
  interval_hours: 2          # как часто "засыпать"
  model_phase1: "haiku"      # извлечение фактов (дешёво)
  model_phase2: "sonnet"     # обновление wiki (умно)
```

Увеличь `interval_hours` если хочешь реже, уменьши если диалоги очень плотные.

### Smart Heartbeat (проактивные уведомления)

```yaml
heartbeat:
  enabled: true
  interval_minutes: 30
  triggers:
    - name: "morning_briefing"
      schedule: "0 9 * * *"        # cron: минута час день месяц день_недели
      prompt: |
        Доброе утро! Подготовь брифинг: ...
      model: "sonnet"
      notify: true                 # всегда уведомлять
    - name: "evening_digest"
      schedule: "0 21 * * *"
      prompt: |...|
      model: "haiku"
      notify: true
    - name: "deadline_check"
      schedule: "0 */4 * * *"       # каждые 4 часа
      prompt: |...|
      model: "haiku"
      notify: "auto"                # LLM решает, стоит ли беспокоить
```

- Добавить триггер — новый элемент в `triggers:` с cron-выражением и промптом
- Убрать — удалить блок или поставить заведомо нереальное расписание
- Тихий режим — `notify: false` (результат запишется в daily, без пуша)

### Knowledge Graph

```yaml
knowledge_graph:
  enabled: true
  run_hour: 1                      # час запуска UTC (1 = 04:00 MSK)
  run_minute: 0
  model: "haiku"                   # дешёвая модель для всех уровней
  max_summaries: 30                # сколько саммари подавать в Уровень 3
  synthesis_schedule:
    daily_phase_days: 14           # первые 14 дней L3 каждый день
    regular_interval_days: 3       # потом каждые 3 дня
```

- **Время запуска** — `run_hour` (UTC). Для 03:00 MSK поставь `0`, для 05:00 MSK — `2`
- **Частота L3** — `daily_phase_days`: сколько дней учиться ежедневно. Потом `regular_interval_days`: интервал в днях
- **Глубина синтеза** — `max_summaries`: сколько последних саммари брать в Уровень 3
- **Отключить полностью** — `enabled: false`
- **Сбросить фазу обучения** — удалить `memory/sessions/.synthesis_state.json`

### Skill Advisor Digest (ежедневная сводка от воркеров)

```yaml
skill_advisor_digest:
  hour: 20                   # час отправки дайджеста (MSK)
```

### Consolidator (сжатие контекста при длинных диалогах)

```yaml
consolidator:
  enabled: true
  max_context_ratio: 0.8     # сжимать при 80% заполнения контекста
  summary_model: "haiku"
  min_messages_to_keep: 5    # последние 5 сообщений не трогать
```

## Что где находится — шпаргалка

| Хочешь изменить... | Файл |
|--------------------|------|
| Как часто Dream обрабатывает память | `agents/me/agent.yaml` → `dream.interval_hours` |
| Время ночного Knowledge Graph | `agents/me/agent.yaml` → `knowledge_graph.run_hour` |
| Частоту Уровня 3 KG | `agents/me/agent.yaml` → `knowledge_graph.synthesis_schedule` |
| Время утреннего брифинга | `agents/me/agent.yaml` → `heartbeat.triggers[0].schedule` |
| Промпт утреннего брифинга | там же → `heartbeat.triggers[0].prompt` |
| Промпт Уровня 1 KG (линковка) | `agents/me/templates/kg_level1_links.md` |
| Промпт Уровня 2 KG (саммари) | `agents/me/templates/kg_level2_summary.md` |
| Промпт Уровня 3 KG (синтез) | `agents/me/templates/kg_level3_synthesis.md` |
| Промпт Dream Phase 1 (факты) | `agents/me/templates/dream_phase1.md` |
| Промпт Dream Phase 2 (wiki) | `agents/me/templates/dream_phase2.md` |
| Личность агента | `agents/me/SOUL.md` |
| Настройки воркеров | `agents/coder/agent.yaml`, `agents/team/agent.yaml` |

## Hot-reload: без перезапуска сервера

После правки `agent.yaml` не нужно останавливать бот. Достаточно:

```
/stop_agent me
/start_agent me
```

или через CLI: `python -m src.cli start-agent me`

Это перезагрузит конфиг, прочитает новые промпты, подхватит изменённое расписание.
Все остальные агенты продолжают работать.

## Ресурсы — сколько это всё стоит

На Claude Pro подписке (~$20/мес, unlimited):

- **Dream**: 12 циклов/день × (haiku + sonnet) = ~12 пар вызовов
- **Smart Heartbeat**: 2-3 вызова/день (briefing, digest, deadline auto)
- **Knowledge Graph**: 2-3 вызова/ночь (L1 + L2 + иногда L3)

Итого ~20 LLM-вызовов в день на одного агента. На Pro подписке это ноль дополнительных расходов — только ограничение rate limit (которое защищается семафором `MAX_CONCURRENT_CLAUDE = 3`).
