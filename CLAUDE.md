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

**HTTP sidecar (`src/http_server.py` + `src/miniapp/`, `src/a2a/`).** A single FastAPI app mounted in the same loop serves:
- Mini App at `/miniapp/` (static `miniapp/index.html` + `assets/`).
- Read API (`src/miniapp/routes.py`): memory tree, files, skills.
- Cockpit API (`src/miniapp/cockpit.py`): stats, agent status, activity feed.
- Actions API (`src/miniapp/actions.py`): POST stop/start/restart, skill install/uninstall/pool refresh — founder-gated.
- A2A protocol (`src/a2a/`): inter-fleet Agent Cards + JSON-RPC server.

All Mini App routes require `Authorization: tma <initData>` + `X-Origin-Agent: <name>` — validated in `src/miniapp/auth.py` via Telegram HMAC against that agent's `bot_token`. `AuthenticatedUser.is_founder` is the gate for destructive ops; `accessible_agents` for read/owner-scoped ops.

**Memory model.** Each agent has a git-versioned `memory/` with `wiki/`, `daily/`, `raw/conversations/`, `log.md`, `stats/audit.jsonl`. `src/dream.py` (4 phases) mines this overnight; `src/knowledge_graph.py` produces `graph.json`; `src/consolidator.py` compacts. Writes to memory are offloaded to `src/git_committer.py` (debounced 2s, async thread pool) — don't call `git` directly from handlers.

**Skills.** `agents/{name}/skills/` holds installed skills (single `.md` or bundle dir with `SKILL.md`). `src/skill_pool.py` syncs the shared git-backed pool. Install/uninstall are hot — no restart. `src/mcp_skill_marketplace.py` exposes the same to agents via MCP.

## Deployment gotchas

**Singleton lock.** `src/main.py::_acquire_singleton_lock` takes an `fcntl.flock` on `/tmp/my-claude-bot-<sha256-of-tokens>.lock`. If a second process starts with the same Telegram tokens (`docker compose up` on top of systemd, two systemd units, forgotten nohup), it exits immediately with the holder's PID — **don't remove or work around this lock**; it's what stops Telegram `Conflict: terminated by other getUpdates request` loops. systemd is the canonical deploy; Docker is local-dev only.

**Do not commit** `webroot/` (ACME challenge artifact from `/setup_dashboard`), `agents/*/memory/` (runtime data), or `.env`.

## Code conventions

- Type hints use `from __future__ import annotations`; `dict`/`list`/`|`-unions are fine (Python 3.10+).
- Comments and docstrings are bilingual — Russian for high-level narrative, English for API-facing and auth/security invariants. Match what you find.
- Async-first: don't introduce blocking I/O; offload to thread pools (`asyncio.to_thread`, or the debounced `git_committer`).
- Tests live in `tests/test_*.py`, use `pytest` + `pytest-asyncio` strict mode. `FakeRuntime`/`FakeAgent` stubs are shared via `tests/test_miniapp_auth.py`; import from there instead of re-building auth scaffolding.
