# Knowledge Graph / Wiki — план приведения к Karpathy LLM-Wiki / OmegaWiki

**Статус:** план, готов к исполнению с новой сессии.
**Дата:** 2026-04-15.
**Автор контекста:** dream + Claude (после фикса self-reference бага).

---

## 0. Зачем этот документ существует

У `me`-агента в my-claude-bot есть wiki-память (`agents/me/memory/wiki/`),
которая должна работать как **персональный knowledge graph фаундера**,
заполняемый из реальных Telegram-разговоров. По образцу:

- **Karpathy LLM-Wiki** — markdown-страницы с YAML, центральный index, wikilinks,
  классификация перед обработкой
  → https://www.analyticsvidhya.com/blog/2026/04/llm-wiki-by-andrej-karpathy/
- **LLM-Wiki v2 / agentmemory (rohitg00)** — confidence scoring, supersession,
  забывание по Эббингхаузу, гибридный поиск, event-driven hooks, crystallization
  → https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2
- **OmegaWiki (skyllwt)** — 9 типов entity, 9 типов typed edges, lint.py с 10
  проверками, навыки читают из wiki и пишут обратно
  → https://github.com/skyllwt/OmegaWiki

Цель: к концу плана пользователь может в Telegram спросить «что мы обсуждали
про Phase 5 две недели назад» — и получить честный ответ на основе графа,
построенного из его же сообщений.

---

## 1. Что уже сделано (фикс 2026-04-15)

Был баг: KG-экстрактор извлекал из daily-логов имена собственной инфраструктуры
агента (`SmartTrigger`, `deadline_check`, `wiki`, `интеграция инструментов`)
как entity. Synthesis-уровень строил вокруг этого «Automated Deadline Management»
кластер с фальшивыми блокерами. Evening_digest читал synthesis и отправлял
пользователю отчёты «система заблокирована 3 дня». Daily получал digest →
KG re-extraction → петля.

**Превентивно (промпты + код):**
- `agents/me/templates/kg_level1_links.md` — добавлен блок-лист имён
  инфраструктуры; явный запрет извлекать работу агента как событие мира.
- `agents/me/templates/kg_level2_summary.md` — то же + игнор секций
  `### [HH:MM] SmartTrigger:`.
- `agents/me/templates/kg_level3_synthesis.md` — то же + «лучше пустой
  synthesis, чем фальшивый».
- `src/smart_heartbeat.py` — добавлен `_is_noise_response()`. NO_DEADLINES /
  NONE / OK / пустые ответы больше не пишутся в daily.

**Очистка существующего мусора:**
- Удалены 4 ложных entity и 4 polluted summaries.
- Сброшены 4 synthesis-страницы до пустых заглушек.
- `graph.json` → `{"edges": []}`.
- `wiki/index.md` переписан без ссылок на ложные сущности.
- Из `daily/2026-04-12..14` вырезаны автогенерированные секции «Связи дня».
- `daily/2026-04-15` переписан как пустая заглушка.

**Что НЕ тронуто:** `log.md`, `roadmap.md`, `profile.md`, `agent.yaml`, прошлые
SmartTrigger-записи в дневных логах (как историческая археология).

---

## 2. Видение vs текущее состояние (gap-анализ)

| Принцип Karpathy / OmegaWiki | Состояние | Приоритет |
|---|---|---|
| Markdown + wikilinks `[[…]]` | ✅ есть | — |
| Центральный `index.md` | ✅ есть | — |
| `graph.json` с typed edges | ✅ есть (11 типов) | — |
| Git versioning памяти | ✅ есть | — |
| Многоуровневая обработка (link → summary → synthesis) | ✅ есть | — |
| **Источник = только user content, не инфраструктура** | 🔴 нет | ★★★ |
| **Богатая типизация entity (9+ типов)** | 🟠 6 плоских типов | ★★ |
| **Supersession (новое утверждение перекрывает старое)** | 🟠 нет | ★★ |
| **Lint-проход (orphan-страницы, висячие ссылки, blocklist-leaks)** | 🟠 нет | ★★ |
| **Query API для пользователя (`/recall <тема>`)** | 🔴 нет | ★★★ |
| **Crystallization сессий (а не дневной дамп)** | 🟡 частично через L2/L3 | ★ |
| **Confidence scoring + забывание** | 🔴 нет | ★ |

---

## 3. Этапы работы (порядок и оценки)

### Этап 1 — Source-фильтрация (★★★, ~30 мин)
KG-экстрактор должен видеть **только user content**, а не весь daily.
Это чинит корневую причину 2026-04-15 бага навсегда (даже если блок-лист
в промптах кто-то ослабит).

### Этап 2 — Query API `/recall` (★★★, ~1.5 ч)
Master-агент получает навык `wiki-search`: BFS по graph.json + чтение
entity-страниц + краткий ответ. Это первый видимый пользователю результат —
проверка «работает ли память».

### Этап 3 — Богатая типизация entity (★★, ~1 ч)
9 типов: `Person, Company, Project, Decision, Idea, Event, Topic, Claim,
Document`. Папки `wiki/people/`, `wiki/projects/`, `wiki/decisions/` и т.д.
Промпт level1 переписывается под эту схему.

### Этап 4 — Supersession + confidence (★★, ~2 ч)
Edge получает поле `supersedes: <previous_edge_id>`. Когда новое утверждение
противоречит старому — старое помечается `superseded`, не удаляется.
Confidence: `direct_user_statement` = 1.0, `inferred` = 0.6.

### Этап 5 — Lint nightly (★★, ~1 ч)
`src/wiki_lint.py` запускается после KG-цикла. Минимум 5 проверок:
1. Entity с именами из инфраструктурного блок-листа
2. Orphan entity-страницы (нет ни одного edge)
3. Висячие edges (endpoint не существует как entity-страница)
4. Дубликаты (одна и та же сущность под двумя именами)
5. Противоречивые edges (A `supports` B И A `contradicts` B без supersession)

### Этап 6 — Crystallization (★, ~3 ч)
Конец сессии (gap 30+ мин или явный `/end`) → дистилляция в structured
факты. Это уже переделка `dream.py` под session-scope, не daily-scope.

### Этап 7 — Confidence decay / забывание (★, ~2 ч)
Edges с `last_seen` старше N дней получают понижение strength по кривой
Эббингхауза. Совсем затухшие переезжают в `wiki/archive/`.

**Этапы 1+2 — обязательный минимум этого спринта.**
**Этапы 3–5 — следующий спринт.**
**Этапы 6–7 — отложено до момента, когда граф реально наполнится данными.**

---

## 4. Этап 1 детально — Source-фильтрация

### Проблема
`src/knowledge_graph.py:link_daily_entities()` сейчас читает весь
`daily/<date>.md` и подаёт целиком в промпт level1. В этом файле вперемешку:
- user messages (`**HH:MM** 👤 ...`)
- ответы агента (`**HH:MM** 🤖 ...`)
- блоки SmartTrigger (`### [HH:MM] SmartTrigger: <name>`)
- ранее добавленные «Связи дня» (если KG уже отработал)

LLM-экстрактор не должен видеть последние два класса.

### Что менять

**Файл:** `src/knowledge_graph.py`

**Шаги:**
1. Добавить функцию `_extract_user_content(daily_text: str) -> str`:
   - Сохраняет блоки `**HH:MM** 👤 …` целиком.
   - Сохраняет блоки `**HH:MM** 🤖 …`, **только если** они идут сразу после
     user-блока (это прямой ответ в диалоге, контекст для извлечения).
   - Полностью вырезает блоки `### [HH:MM] SmartTrigger: …` (от заголовка
     до следующего `### [` или `**HH:MM**` или EOF).
   - Полностью вырезает секции `## Связи дня …` и `### Упомянутые сущности …`.
2. В `link_daily_entities` вызывать `_extract_user_content(daily_content)`
   до подстановки в промпт.
3. Если после фильтрации осталось < 50 символов — лог `KG L1: дневной
   user-контент пуст, пропускаю` и выходим (как сейчас при пустом daily).
4. Аналогичную фильтрацию сделать в `summarize_day` (level2).
5. **Не трогать level3** — он работает с уже отфильтрованными summaries.

**Тесты:** `tests/test_user_content_filter.py` — таблица из 6 кейсов:
- Только user → весь текст
- Только SmartTrigger → пусто
- User + SmartTrigger вперемешку → только user-блоки + прямые ответы
- User + Связи дня → только user-блоки
- Пустой daily → пусто
- 🤖 без предшествующего 👤 (фоновый ответ) → вырезается

### Acceptance criteria этапа 1
- [ ] `link_daily_entities` на `daily/2026-04-12.md` (где 95% SmartTrigger
      шум + ~5 строк реального диалога) извлекает только entity, упомянутые
      в этих 5 строках. На моей текущей проверке это `Split status display`,
      `Knowledge Graph`, `SkillCreator` — реальные обновления, которые dream
      обсуждал с агентом 12 апреля.
- [ ] Юнит-тесты фильтра — все зелёные.
- [ ] Запуск `python -m src.knowledge_graph --agent agents/me --date 2026-04-12`
      даёт чистый JSON без `SmartTrigger`, `deadline_check`, `wiki`.

---

## 5. Этап 2 детально — Query API `/recall`

### Цель
Первая видимая пользователю функция, которая доказывает, что память не
бутафория. В Telegram: `/recall Phase 5` → агент возвращает 5–10 наиболее
релевантных entity и связей, плюс цитаты из дневных логов.

### Что строим

**Файл 1:** `agents/me/skills/wiki-search.md` (новый навык для master-агента)

Алгоритм навыка (детерминированно, без LLM-вызова кроме финального reranking):
1. Принимает `query: str`.
2. Читает `agents/me/memory/graph.json` и `wiki/entities/*.md`,
   `wiki/concepts/*.md`, `wiki/synthesis/*.md`.
3. Делает grep по entity-именам и заголовкам страниц (BM25-lite через
   простой Python без зависимостей: term frequency × inverse doc frequency).
4. Для топ-3 entity делает BFS глубиной 1 по graph.json — добавляет соседей.
5. Для каждой entity берёт `last_seen` и тянет цитату из соответствующего
   `daily/<date>.md` (поиск по имени entity в файле, окно ±2 строки).
6. Возвращает структурированный ответ: список `{entity, тип, связи, цитата, дата}`.
7. Финальный шаг: master-агент сам формулирует human-readable ответ из этих
   данных. Здесь LLM-вызов уже неизбежен, но контекст уже отфильтрован.

**Файл 2:** `src/wiki_search.py` (Python helper, не LLM)
- Функции `search(query, agent_dir) -> list[Hit]`, `bfs(graph, start, depth)`,
  `quote_from_daily(entity_name, date, agent_dir) -> str`.
- Без внешних зависимостей. BM25 на чистом Python — ≤100 строк.

**Файл 3:** регистрация навыка в `agents/me/agent.yaml`:
```yaml
skills:
  - "document-analysis"
  - "web-research"
  - "wiki-search"   # ← новое
```

**Файл 4:** `src/command_router.py` — добавить slash-команду `/recall`
которая просто проксирует `/recall <query>` → master-агенту с подсказкой
«используй навык wiki-search».

### Acceptance criteria этапа 2
- [ ] `/recall Phase 5` в Telegram возвращает упоминания «Phase 5» из
      существующих заметок (если они есть) или честный ответ «в графе пока
      нет упоминаний Phase 5» (если граф пуст после очистки).
- [ ] `wiki-search` работает на пустом графе без падения.
- [ ] Поиск по entity, которой нет, возвращает пустой результат, а не
      галлюцинирует.
- [ ] Юнит-тест: добавляем фейковые `wiki/entities/Phase5.md` + edge в
      `graph.json` → `wiki_search.search("Phase 5")` находит.

---

## 6. Что отложено и почему

- **Этапы 6–7 (crystallization, забывание)** — пока граф пуст, эти фичи
  не на чем тестировать. Возвращаемся, когда наберётся 2+ недели реальных
  user-сообщений.
- **Гибридный поиск (vector + BM25)** — добавит зависимость на embeddings.
  BM25 хватит на первой версии. Векторы — когда появится >500 entity.
- **Obsidian-совместимость** — wiki уже в формате Obsidian-`[[links]]`,
  но мы не запускаем Obsidian. Если dream захочет открывать вручную —
  оно уже работает.
- **Web UI / Telegram Mini App для wiki** — уже в backlog ROADMAP.md,
  не дублируем.

---

## 7. Заметки для входа в новую сессию

Если читаешь этот файл с нуля — вот что нужно знать перед работой:

1. **Корневая директория агента:** `agents/me/memory/`. Всё локально, никаких
   внешних wiki, URL, API. `wiki/` — это `./wiki/` относительно cwd агента.
2. **Запуск нужного KG-цикла вручную для теста:**
   ```bash
   cd ~/my-claude-bot && python -m src.knowledge_graph \
     --agent agents/me --date 2026-04-12
   ```
3. **Сервис my-claude-bot перезапускается через:**
   ```bash
   sudo systemctl restart my-claude-bot
   ```
   (но Python-промпты подхватываются на каждый цикл без рестарта; рестарт
   нужен только после правок `*.py`).
4. **Текущее состояние памяти после очистки 2026-04-15:** entity-страниц нет,
   synthesis-страницы пустые, graph.json пуст. Это нормальный старт. После
   первого реального разговора пользователя с агентом и ночного KG-цикла
   должны появиться первые entity.
5. **Проверить, что фикс работает:** через 24 часа после нескольких реальных
   разговоров посмотреть `wiki/entities/` — там должны появиться имена людей,
   проектов, тем, которые dream обсуждал, и НЕ должно быть `SmartTrigger`,
   `deadline_check`, `wiki`, `интеграция инструментов`.
6. **Ключевые файлы для этого плана:**
   - `src/knowledge_graph.py` — KG L1/L2/L3 пайплайн
   - `src/smart_heartbeat.py` — фоновые триггеры
   - `agents/me/templates/kg_level{1,2,3}_*.md` — промпты экстракции
   - `agents/me/memory/wiki/` — целевая wiki
   - `agents/me/memory/graph.json` — граф
7. **Что НЕ делать:**
   - Не коммитить `ROADMAP.md` и `MEMORY_SYNC.md` (local-only).
   - Не трогать прошлые `daily/<date>.md` логи как «исторические записи».
   - Не упрощать промпты L1/L2/L3 без сохранения блок-листа инфраструктуры.
   - Не добавлять `SmartTrigger`, `deadline_check`, `wiki`, `memory`, `daily`,
     `dispatcher`, `bus`, `dream`, `heartbeat`, `knowledge_graph` как entity
     ни вручную, ни через промпт.

---

## 8. Definition of Done (для всего плана)

План считается выполненным, когда:

- [ ] Этап 1 закрыт: KG-фильтр работает, тесты зелёные, на исторических
      dailies реальные entity находятся, инфраструктурные — нет.
- [ ] Этап 2 закрыт: `/recall <тема>` в Telegram отвечает осмысленно.
- [ ] За 7 дней реальной работы (после деплоя этапов 1+2) в графе
      появляется ≥10 реальных entity (люди / проекты / решения / темы),
      и 0 инфраструктурных.
- [ ] Evening_digest перестал писать «система заблокирована» — пишет
      реальные итоги дня или «существенных событий не было».
- [ ] Dream может за 30 секунд вспомнить через `/recall`, что обсуждалось
      на прошлой неделе.

После DoD — открываем этапы 3–5 (типизация + supersession + lint).
