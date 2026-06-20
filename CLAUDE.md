# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run commands

```bash
# Tests (pytest w/ asyncio strict mode; 800+ tests, ~20s)
python3 -m pytest tests/ -q
python3 -m pytest tests/test_miniapp_actions.py -x --tb=short     # single file
python3 -m pytest tests/test_foo.py::TestClass::test_case         # single test

# Restart the running bot (systemd user-unit is the canonical deploy)
systemctl --user restart my-claude-bot
journalctl --user -u my-claude-bot -f

# Update in place (git pull + deps + restart)
./update.sh

# Smoke-import after backend changes before restart
python3 -c "from src.main import main; print('ok')"
```

There is no linter or formatter configured — follow the style of surrounding code.

## Architecture

**Entry point.** `src/main.py:main` → `async_main` boots a single asyncio loop that runs N Telegram bots (one per agent), an HTTP sidecar (FastAPI), background loops (Dream, Heartbeat, Cron, Dispatcher, Delegation, KnowledgeGraph), and a debounced git committer. `uvloop` is used when available.

**Fleet model.** Every agent is an isolated folder `agents/{name}/` with `agent.yaml`, `SOUL.md`, `skills/`, `memory/`. `me` is the master (full tool access); every other agent is sandboxed to its own folder via a scoped tool allowlist. Agents communicate only through `FleetBus` (pub/sub, topics like `agent:coder`, `telegram:me`) — no direct calls. **`agents/` is user data, never modify files there from code changes; tests use `tmp_path`.**

**Key classes to know before editing (`src/`):**
- `main.py::FleetRuntime` — global context held in `app.state`; owns `agents`, `workers`, `bridges`, `tasks`, and hot-reload primitives `start_agent(name)` / `stop_agent(name)` that the Mini App and Telegram commands both call.
- `bus.py::FleetBus` — in-process async message bus; every cross-agent signal goes through it.
- `orchestrator.py::Orchestrator` — master-to-worker delegation routing.
- `agent_worker.py::AgentWorker` — per-agent turn executor; holds `_active_tasks`, `_pending_followups` (mid-turn injection buffer).
- `agent.py::Agent` — dataclass loaded from `agent.yaml`; `agent.bot_token` expands `${ME_BOT_TOKEN}`-style vars.
- `telegram_bridge.py::TelegramBridge` — PTB `Application` per agent, wires `/stop`, `/restart`, file round-trip, voice, etc.
- `max_bridge.py::MaxBridge` — опциональный **второй транспорт** (мессенджер МАКС/VK на `maxapi`). Поднимается рядом с Telegram, только если у агента задан `max_bot_token`. См. «Multi-transport» ниже.

**Multi-transport (Telegram + МАКС).** Мозг флота транспортно-нейтрален: телеграм-специфика заперта в `TelegramBridge`. Второй транспорт — МАКС — добавлен как параллельный мост, не ломая Telegram-путь:
- **Routing.** Каждый bridge помечает входящее `metadata["transport"]` (`telegram` по умолчанию, `max` у MaxBridge). `AgentWorker` шлёт ВСЕ ответы в `f"{transport}:{name}"`, а не хардкодом `telegram:` — ответ уходит туда, откуда пришло. Дефолт `"telegram"` сохраняет полную обратную совместимость.
- **Включение.** `Agent._resolve_max_bot_token` берёт токен из `max_bot_token` в `agent.yaml` ИЛИ авто-выводит из env `{NAME}_MAX_BOT_TOKEN` (zero-config, без правки user-yaml). Нет токена → мост не поднимается.
- **Опциональная зависимость.** `maxapi` импортируется лениво в `_maybe_start_max_bridge` (main.py) и в `MaxBridge.run()`. Не установлен → graceful degrade (агент остаётся в Telegram), как `bwrap` для sandbox.
- **Подписки шины.** `_maybe_start_max_bridge` подписывает `max:{name}`; `stop_agent` отписывает (идемпотентно). Оба пути старта (boot + hot-reload) идут через один хелпер.
- **v1-объём MaxBridge.** Текст в обе стороны + `allowed_users` (fail-closed) + лог в `memory/`. **Live-стриминг ответа** (`_MaxStatus`, сверено на живом токене): на `processing_started` шлём статус «⏳ Думаю…», на `text_delta` троттленно (≈1.2с) дописываем его через `edit_message`, на финальном `OUTBOUND` финализируем НА МЕСТЕ; «хвост» сверх лимита и файлы досылаем новыми сообщениями. Идентичный финальный edit пропускаем (иначе «message is not modified» → дубль ответа). Фоновый «печатает…» работает параллельно (анимация в шапке vs текст в ленте — не конфликтуют). **Реальная отправка файлов** (`_send_files`/`_upload_attachment`, сверено на живом токене): тип по расширению → `InputMedia` → `upload_media` → `send_message(attachments=...)`; при отказе медиа-сервера (напр. битая картинка → нет `photos`) ретрай как `type=file`, затем текстовая пометка. Инвариант: `clear_outbox` через `try/finally` после отправки (МАКС-ответ роутится только в `max:{name}`, Telegram outbox не очистит). Богатый набор команд (`/stop` и т.п.) — пока только в Telegram (отмена хода без терминального OUTBOUND подчищается на следующем `processing_started`).

**HTTP sidecar (`src/http_server.py` + `src/miniapp/`, `src/a2a/`).** A single FastAPI app mounted in the same loop serves:
- Mini App at `/miniapp/` (static `miniapp/index.html` + `assets/`).
- Read API (`src/miniapp/routes.py`): memory tree, files, skills.
- Cockpit API (`src/miniapp/cockpit.py`): stats, agent status, activity feed.
- Actions API (`src/miniapp/actions.py`): POST stop/start/restart, skill install/uninstall/pool refresh — founder-gated.
- A2A protocol (`src/a2a/`): inter-fleet Agent Cards + JSON-RPC server.

All Mini App routes require `Authorization: tma <initData>` + `X-Origin-Agent: <name>` — validated in `src/miniapp/auth.py` via Telegram HMAC against that agent's `bot_token`. `AuthenticatedUser.is_founder` is the gate for destructive ops; `accessible_agents` for read/owner-scoped ops.

**Memory model.** Each agent has a git-versioned `memory/` with `wiki/`, `daily/`, `raw/conversations/`, `log.md`, `stats/audit.jsonl`. `src/dream.py` (4 phases) mines this overnight; `src/knowledge_graph.py` produces `graph.json`; `src/consolidator.py` compacts. Writes to memory are offloaded to `src/git_committer.py` (debounced 2s, async thread pool) — don't call `git` directly from handlers.

**Skills.** `agents/{name}/skills/` holds installed skills (single `.md` or bundle dir with `SKILL.md`). `src/skill_pool.py` syncs the shared git-backed pool. Install/uninstall are hot — no restart. `src/mcp_skill_marketplace.py` exposes the same to agents via MCP.

**Sandbox policy (workers).** Worker-агенты получают `Bash` + `Read/Write/Edit/Glob/Grep/WebSearch/WebFetch` в allowed_tools и работают с cwd=`agents/{name}/memory/`. Scope ограничен двумя слоями: (1) hook — `src/sandbox.py` как PreToolUse CLI-хук блокирует абсолютные пути вне `agents/{name}/` и `/tmp` `/usr/bin` `/bin` `/usr/lib` `/dev/null`; относительные пути всегда ok. (2) bubblewrap — kernel-enforced isolation для Bash (graceful degrade если `bwrap` не установлен). Оба включаются по умолчанию для новых агентов через `AGENT_YAML_TEMPLATE` в `src/agent_manager.py`. **Runtime safety net**: `Agent._augment_worker_file_tools` (agent.py) в runtime добавляет `Bash`+`Edit` любому worker'у с `sandbox.enabled`, если они не в `allowed_tools` — это даёт zero-config upgrade для старых yaml без мутации user-files. Master и явно unsandboxed агенты не augment'ятся. Легитимные исключения — `me` (master) и `coder` (trusted dev, sandbox off). Policy проверяется в `tests/test_agent_policy.py` (yaml source of truth) и `tests/test_agent.py::TestAugmentWorkerFileTools` (runtime). PDF/изображения/архивы/аудио обрабатываются прямо в своей memory/ через `pdftoppm`/`ffmpeg`/`convert`/`unzip` (в `/usr/bin` — always-allowed).

## Deployment gotchas

**Singleton lock.** `src/main.py::_acquire_singleton_lock` takes an `fcntl.flock` on `/tmp/my-claude-bot-<sha256-of-tokens>.lock`. If a second process starts with the same Telegram tokens (`docker compose up` on top of systemd, two systemd units, forgotten nohup), it exits immediately with the holder's PID — **don't remove or work around this lock**; it's what stops Telegram `Conflict: terminated by other getUpdates request` loops. systemd is the canonical deploy; Docker is local-dev only.

**Do not commit** `webroot/` (ACME challenge artifact from `/setup_dashboard`), `agents/*/memory/` (runtime data), or `.env`.

## Code conventions

- Type hints use `from __future__ import annotations`; `dict`/`list`/`|`-unions are fine (Python 3.10+).
- Comments and docstrings are bilingual — Russian for high-level narrative, English for API-facing and auth/security invariants. Match what you find.
- Async-first: don't introduce blocking I/O; offload to thread pools (`asyncio.to_thread`, or the debounced `git_committer`).
- Tests live in `tests/test_*.py`, use `pytest` + `pytest-asyncio` strict mode. `FakeRuntime`/`FakeAgent` stubs are shared via `tests/test_miniapp_auth.py`; import from there instead of re-building auth scaffolding.
