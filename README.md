# My Claude Bot

[🇷🇺 Русская версия](README.ru.md)

**What it is in 5 sentences.** My Claude Bot turns your Claude Pro ($20/mo) or Claude Max ($100/mo, $200/mo) subscription into a fleet of AI Telegram agents centred on `me` — the master orchestrator and entry point that configures and delegates to sandboxed workers: `coder`, `team` hub, `archivist`, plus unlimited custom agents you can spin up on the fly. Only `me` has unrestricted access; every worker agent is locked inside its own `agents/{name}/` folder with a scoped tool allowlist and cannot read or write anything outside it. Agents share a MessageBus, remember everything in git-versioned wiki memory, and self-improve overnight via Dream cycles that analyse usage and propose new skills or schema changes. Works in personal chats and groups, handles files and voice messages, installs community skills from a shared marketplace, and runs unattended on any cheap VPS. No API bills, no per-call charges — install with one command, update with one command, and pay a fixed monthly fee for everything.

```
                     Telegram  (DMs + groups + topics)
                                  │
                                  ▼
                      ┌──────────────────────┐
                      │          me          │ ◄── master / orchestrator
                      │   full access, all   │     entry point, configures
                      │   tools, creates     │     and delegates to workers
                      │   other agents       │
                      └──────────┬───────────┘
                                 │
                                 ▼ delegates via MessageBus
            ┌──────────┬─────────┴─────────┬────────────┐
            │          │                   │            │
            ▼          ▼                   ▼            ▼
          coder       team             archivist     custom…
          (dev)      (group)             (docs)     (your own)
            │          │                   │            │
            └──────────┴───────────────────┴────────────┘
             sandboxed workers: each locked to agents/{name}/
             with scoped tool allowlist, cannot touch other folders

                                 │
               ┌─────────────────┼──────────────────┐
               ▼                 ▼                  ▼
         ┌──────────┐     ┌────────────┐     ┌────────────┐
         │Claude Pro│     │Wiki memory │     │  Skills +  │
         │  or Max  │     │ per agent, │     │ Pool market│
         │(flat fee)│     │ git-backed │     │            │
         └──────────┘     └────────────┘     └────────────┘

    Background: Dream (4 phases) · Knowledge Graph · Smart Heartbeat · Cron
                SkillAdvisor / SchemaAdvisor · Consolidator · Sandbox
```

## Features

- **Master + workers** -- `me` is the master orchestrator and entry point with full access, configures everything and delegates to workers; `coder` (dev workflow), `team` (group hub), and `archivist` (domain-agnostic document archive) ship as ready-to-use sandboxed workers
- **Multi-agent fleet** -- add unlimited custom agents on top, each with its own Telegram bot, SOUL, and skills; all non-master agents inherit the sandbox
- **Agent delegation** -- master/worker hierarchy, Orchestrator routes messages through the MessageBus
- **Sandbox** -- every non-master agent runs inside its own `agents/{name}/` folder with a scoped tool allowlist, cannot read or write outside it; only `me` has unrestricted access
- **MessageBus** -- async message bus between agents (pub/sub, broadcast, prefix routing)
- **Streaming responses** -- text appears in Telegram as it's generated, not as a single block
- **Dream Memory** -- background 3+ phase memory processing (fact extraction, wiki updates, pattern analysis)
- **Knowledge Graph** -- 3-level nightly memory linking pipeline (Obsidian-style `[[links]]`, daily summaries, graph synthesis)
- **SkillAdvisor / SchemaAdvisor** -- agents analyze usage patterns and proactively propose new skills or schema improvements, never applied automatically
- **Skill Pool marketplace** -- install community skills from a shared pool (`/poolskills`, `/installskill`), hot-reload without restart
- **SkillCreator** -- dynamic skill creation via the orchestrator on demand
- **Smart Heartbeat** -- proactive agent with cron triggers: checks tasks, executes, decides whether to notify
- **Smart Context Management** -- budget system with semantic wiki search, keeps context tight under 200K
- **Cron jobs** -- periodic tasks with cron expressions (digests, summaries, monitoring)
- **MCP servers** -- connect Todoist, GitHub, Google Calendar, and any MCP via config
- **Wiki memory** (Karpathy model) -- automatically records people, decisions, ideas with git versioning and rollback
- **Skills with frontmatter** -- agentskills.io spec alignment, progressive disclosure, multi-file bundles, dependency checks
- **Hook system, Command Guard, Consolidator** -- execution pre/post hooks, command allow/deny policies, memory compaction
- **Voice messages** -- transcription via Deepgram API (Nova-3)
- **Files** -- receive and analyze documents, photos via Telegram (up to 20MB); file round-trip outbox pattern for sending files back
- **Group chats** -- dual-mode (DM + groups), silent logging, isolated memory, topic support
- **Hot agent management** -- `/create_agent`, `/clone_agent`, `/set_access`, `/start_agent`, `/stop_agent` without service restart
- **Onboarding** -- language selection + profile setup on first launch, `/start` auto-registers first client
- **i18n** -- English and Russian interface, language auto-saved per user

## Quick Start

```bash
git clone https://github.com/dream77r/my-claude-bot.git && cd my-claude-bot && ./setup.sh
```

The script will:
1. Check dependencies (Python, Claude CLI)
2. Ask for your bot token (with instructions to get one from @BotFather)
3. Ask for your Telegram ID (with instructions to get it from @userinfobot)
4. Ask for resource limits (memory, CPU) based on your server
5. Install packages, create `.env`, set up auto-restart service
6. Start the bot

Open Telegram and message your bot — it handles everything else (onboarding, language, settings).

**Prerequisites:** Python 3.10+, Claude CLI (installed and authorized), Claude Pro or Claude Max subscription.

## Updating

```bash
cd ~/my-claude-bot && git pull && ./update.sh
```

One command: pulls the latest code, updates dependencies if needed, and restarts the service. Your data is safe -- `.env`, agent memory, `SOUL.md`, and settings are never touched.

## Running with systemd (recommended)

```bash
cp .env.example .env
# fill in .env: bot token, Telegram ID

# Create a user-level systemd service
mkdir -p ~/.config/systemd/user
cp my-claude-bot.service ~/.config/systemd/user/
# Edit the service file: set WorkingDirectory and Environment paths

systemctl --user daemon-reload
systemctl --user enable my-claude-bot   # autostart on boot
systemctl --user start my-claude-bot    # start now
sudo loginctl enable-linger $USER       # keep running after logout
```

The bot automatically:
- restarts on crash (5 sec delay)
- starts on server reboot
- is limited by memory (1 GB) and CPU (2 cores)
- sends a Telegram notification on every (re)start

**Useful commands:**
```bash
systemctl --user status my-claude-bot    # status
journalctl --user -u my-claude-bot -f    # real-time logs
systemctl --user restart my-claude-bot   # manual restart
```

## Running with Docker (local dev only)

> **⚠ Don't run Docker alongside systemd.** Both read the same `.env`, so both
> would poll Telegram with the same bot token — Telegram kicks both processes
> with `Conflict: terminated by other getUpdates request`. The bot now bails
> out on startup if another instance holds the same token, but that means one
> of the two will simply refuse to start. Pick one deployment, not both.
>
> If you ran `./setup.sh` you already have the systemd unit. To use Docker
> instead, stop systemd first: `systemctl --user disable --now my-claude-bot`.

Docker is useful for local development (e.g. on macOS/Windows) or isolated
testing. For production on Linux, use systemd — it's what `./setup.sh`
installs and what `/restart` and `/setup_dashboard` expect.

```bash
cp .env.example .env
# fill in .env: HOST_HOME, bot token, Telegram ID

docker compose up -d --build
```

Note: Claude CLI must be accessible inside the container (mounted via volumes in `docker-compose.yml`).

## Voice Messages

The bot transcribes voice messages via Deepgram API (Nova-3 model). Cost ~$0.0043/min, free tier available ($200 credit).

**Setup (two options):**

1. **Via chat** -- send the bot: "Here's my Deepgram key: your_key"
2. **Via `.env`** -- add `DEEPGRAM_API_KEY=your_key`

Get a key: https://console.deepgram.com/

**How it works:** Telegram voice (OGG) → download → Deepgram API → text → processed as a regular message.

## Group Chats

The bot works in two modes depending on `chat.type`:
- **Private** -- full access, all settings, personal memory
- **Group/Supergroup** -- isolated mode, responds only on @mention or reply

**Group features:**
- Silent logging of ALL messages (builds context without responding)
- Isolated memory per group (`memory/groups/{chat_id}/`)
- Separate system prompt (no access to owner's personal data)
- Forum topic support (responds in the correct topic, can be restricted to one)
- DM-based setup: when added to a group, the bot DMs the owner asking how to behave
- Command permissions: admin-only commands (`/model`, `/restore`) vs public (`/help`, `/status`)

## MCP Servers (Todoist, GitHub, etc.)

Claude CLI supports MCP (Model Context Protocol) servers -- external tools the agent can use directly.

**Setup:** uncomment the `mcp_servers` section in `agents/me/agent.yaml`:

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

Add tokens to `.env`:
```bash
TODOIST_API_TOKEN=your_todoist_token
GITHUB_PERSONAL_ACCESS_TOKEN=your_github_token
```

Where to get tokens:
- Todoist: https://todoist.com/app/settings/integrations/developer
- GitHub: https://github.com/settings/tokens (scopes: repo, read:org)

You can connect any MCP server -- the format matches Claude CLI config.

## Cron Jobs

Periodic tasks with cron expressions. The agent runs a prompt on schedule and notifies via Telegram.

**Config in `agent.yaml`:**

```yaml
cron:
  - name: "daily_digest"
    schedule: "0 21 * * *"          # every day at 21:00
    prompt: "Summarize the day based on the daily note"
    model: "haiku"
    notify: true
  - name: "weekly_summary"
    schedule: "0 9 * * 1"           # every Monday at 9:00
    prompt: "Create a weekly summary: key decisions, progress, blockers"
    model: "sonnet"
    notify: true
```

Supported cron expressions: `*`, `*/N`, `N-M`, `N,M,K`, exact values. Format: `minutes hours day month weekday`.

## Requirements

- Python 3.10+ or Docker
- Claude CLI (installed and authorized)
- Claude Pro or Claude Max subscription (any tier)
- Telegram bot (create via @BotFather)
- Deepgram API key (optional, for voice)
- Node.js/npx (optional, for MCP servers)

## Architecture

```
Telegram User → TelegramBridge → MessageBus → AgentWorker → Agent.call_claude()
                     ↑                                            ↓
                bus listener ← ── ── ── ── ── ← ── ── ── ── response/streaming
                     ↓                                            ↓
              StatusMessage (streaming, tool hints)         Delegation → worker agent
                                                            (sandboxed)

Background processes:
  Dream loop (every N hours)  → Phase 1  (haiku: fact extraction)
                               → Phase 2  (sonnet: wiki updates)
                               → Phase 3  (SkillAdvisor: pattern analysis → skill suggestions)
                               → Phase 3b (SchemaAdvisor: vault analysis → schema suggestions, archivist only)
  Knowledge Graph (nightly)   → Level 1 (link daily notes with [[wiki]] refs)
                               → Level 2 (daily summaries with cross-references)
                               → Level 3 (graph synthesis, adaptive schedule)
  Smart Heartbeat (triggers)  → Check → Execute → Evaluate → Notify?
  Cron (on schedule)          → Execute prompt → Notify
  Consolidator                → Memory compaction when context approaches limits
```

## Project Structure

```
src/
  main.py               -- entry point, fleet launcher
  agent.py              -- Agent: config, system prompt, Claude calls, MCP
  agent_worker.py       -- AgentWorker: bridges Agent with MessageBus
  agent_manager.py      -- Agent Manager: create/list/validate agents
  telegram_bridge.py    -- Telegram handlers, message aggregation, streaming
  bus.py                -- MessageBus: pub/sub bus on asyncio.Queue
  orchestrator.py       -- message routing between agents
  delegation.py         -- master/worker delegation hierarchy
  dispatcher.py         -- background message dispatch with explicit chat routing
  dream.py              -- Dream Memory: 4-phase background processing
  knowledge_graph.py    -- 3-level nightly memory linking pipeline
  skill_advisor.py      -- Dream Phase 3: pattern analysis → skill suggestions
  schema_advisor.py     -- Dream Phase 3b: vault analysis → schema suggestions (archivist)
  skill_pool.py         -- community skill marketplace (install from shared pool)
  skill_creator.py      -- dynamic skill creation via orchestrator
  smart_heartbeat.py    -- proactive agent with cron triggers
  heartbeat.py          -- legacy heartbeat (simple interval)
  consolidator.py       -- memory compaction when context approaches limits
  hooks.py              -- pre/post execution hooks
  command_guard.py      -- command allow/deny policies
  sandbox.py            -- filesystem isolation for worker agents
  cron.py               -- Cron: scheduled periodic tasks
  memory.py             -- Karpathy Wiki: profile, wiki/, daily notes, git-backed
  input_sanitizer.py    -- input validation and sanitization
  ssrf_protection.py    -- SSRF guard for WebFetch/URL handling
  audit.py              -- audit log for security-relevant operations
  metrics.py            -- metrics collection
  checkpoint.py         -- session checkpointing
  command_router.py     -- 4-level command router
  cli.py                -- CLI interface for agent management
  tool_hints.py         -- tool status messages
  voice_handler.py      -- voice via Deepgram API
  file_handler.py       -- file upload/download with outbox round-trip
  i18n.py               -- English/Russian locale system

agents/me/                    -- strategic advisor (master)
agents/coder/                 -- technical agent (dev tools: Bash, Edit, Grep)
agents/team/                  -- team hub (groups, task-tracking, research)
agents/archivist/             -- document archive, domain-agnostic
  agent.yaml                  -- config (5 skills, schema_advisor, vault-lint cron)
  skills/                     -- vault-init, document-ingest, archive-search,
                                 vault-lint, schema-evolve
  memory_template/            -- empty public scaffold (CLAUDE.md, .vault-config.json)
  meta-templates/             -- templates for generating domain-specific templates
  examples/                   -- reference domains (small-business, ...)

Each agent folder contains: agent.yaml, SOUL.md (gitignored, local),
skills/, templates/, memory/ (gitignored, seeded from memory_template/ on startup).
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome + onboarding on first launch, auto-registers first client |
| `/help` | Command reference |
| `/newsession` | Reset context (new Claude session) |
| `/stop` | Stop current request (works even when agent is busy) |
| `/status` | Agent status, session info, last memory backup |
| `/memory` | Memory change history (git log) |
| `/restore` | Roll back memory to a previous version |
| `/dream` | Manually trigger Dream memory processing |
| `/model` | Switch Claude model (Haiku/Sonnet/Opus) |
| `/agents` | List all agents and their status |
| `/create_agent` | Create a new agent via interactive wizard |
| `/clone_agent` | Copy SOUL, skills, and settings from an existing agent |
| `/start_agent` | Start an agent by name |
| `/stop_agent` | Stop an agent by name |
| `/set_access` | Manage agent access on the fly (add/remove allowed users) |
| `/poolskills` | Browse skills available in the community skill pool |
| `/installskill` | Install a skill from the pool (hot-reload) |
| `/restart` | Restart the platform (applies code updates) |

## Customizing an Agent Without Losing Updates (`agent.local.yaml`)

`agents/<name>/agent.yaml` ships with the bot and is overwritten on every
`git pull`. To customize an agent (own `allowed_users`, a different model,
your own `system_prompt`) without merge conflicts, create an **untracked**
overlay next to it:

```yaml
# agents/me/agent.local.yaml  (gitignored)
claude_model: opus
allowed_users:
  - 44117786
system_prompt: |
  Overridden prompt just for my install.
```

Fields in `agent.local.yaml` are deep-merged over `agent.yaml` when the
agent loads: `dict`s merge recursively, lists/scalars replace. That lets
you *narrow* `allowed_users` to a subset, not just extend it.

If you already edited `agent.yaml` by hand before this shipped, `./update.sh`
runs a one-shot migration on first execution: it extracts your diffs into
`agent.local.yaml` and rolls `agent.yaml` back to upstream. No action needed
on your side.

## Agent Config (agent.yaml)

```yaml
name: "me"
display_name: "Strategic Advisor"
bot_token: "${ME_BOT_TOKEN}"
system_prompt: |
  You are a personal strategic advisor...

skills:
  - "document-analysis"
  - "web-research"
allowed_users:
  - ${FOUNDER_TELEGRAM_ID}
claude_model: "sonnet"
claude_flags:
  - "--allowedTools"
  - "Read,Write,Glob,Grep,WebSearch,WebFetch"

# MCP servers (optional)
mcp_servers:
  todoist:
    command: "npx"
    args: ["-y", "@anthropic/mcp-todoist"]
    env:
      TODOIST_API_TOKEN: "${TODOIST_API_TOKEN}"

# Dream Memory -- background memory processing
dream:
  interval_hours: 2
  model_phase1: "haiku"
  model_phase2: "sonnet"

# Heartbeat -- proactive tasks
heartbeat:
  enabled: true
  interval_minutes: 30

# Cron -- periodic tasks
cron:
  - name: "daily_digest"
    schedule: "0 21 * * *"
    prompt: "Summarize the day"
    model: "haiku"
    notify: true
```

## Adding a New Agent

### Via Telegram (recommended)

Send `/create_agent` to your main bot. The wizard will guide you through 5 steps:

1. Agent name (latin, for the folder)
2. Display name
3. Bot token (create a new bot via @BotFather)
4. Role description
5. Claude model (haiku / sonnet / opus)

The agent starts immediately via hot-reload -- no restart needed.

### Via CLI

```bash
python -m src.cli create-agent    # interactive wizard
python -m src.cli list-agents     # list all agents
python -m src.cli validate        # check configs
```

### Managing agents

| Command | Description |
|---------|-------------|
| `/agents` | List all agents with status (running/stopped/no token) |
| `/create_agent` | Create a new agent via Telegram wizard |
| `/start_agent name` | Start an agent |
| `/stop_agent name` | Stop an agent |

Each agent is a separate Telegram bot with isolated memory. The Orchestrator automatically routes messages through the MessageBus.

## Security

- Tokens stored in `.env` (not tracked by git, file permissions 600)
- Bot access restricted to specified Telegram IDs (`allowed_users`)
- Claude CLI runs with a limited set of tools (`allowedTools`)
- **Master/worker isolation:** only `me` (the master orchestrator) has unrestricted access; every other agent runs inside its own `agents/{name}/` folder with a scoped tool allowlist and cannot read or write outside it
- **Bash sandbox (optional, Linux):** opt-in kernel-level isolation via bubblewrap. Add `sandbox.bubblewrap: true` to a worker's `agent.yaml`, install `bwrap` (`sudo apt-get install -y bubblewrap`), and the Claude CLI runs bash commands in a namespace-isolated view of the filesystem — protecting against clever bash that would otherwise bypass path-based hooks. Defense-in-depth on top of the existing `sandbox.py` hook.
- Each agent's memory is isolated (its own `memory/`)
- Git-versioned memory with rollback capability (`/restore`)
- Group chats: owner's personal data never exposed in group system prompts
- Resource limits: memory (1 GB), CPU (2 cores), max processes (100)
- Restart notification via Telegram on every service (re)start
- Claude CLI path auto-detected via `PATH` or `CLAUDE_CLI_PATH` env var

## Multi-user setup

Multiple users can run their own bot instances on the same server:
- Each user creates their own bot via @BotFather (unique token)
- Each user has their own `.env`, `agents/`, and systemd service
- User-level systemd services are fully isolated
- No conflicts as long as bot tokens are different

## Roadmap

- **Phase 1 (done):** personal assistant, files, voice, memory, onboarding, git-backed wiki
- **Phase 2 (done):** MessageBus, Orchestrator, Dream Memory, Heartbeat, Skills frontmatter, Consolidator, Hook system, Command Guard
- **Phase 3 (done):** multi-agent fleet, group chats, topics, streaming, MCP, cron, i18n
- **Phase 4 (done):** agent delegation (master/worker), semantic wiki search, Smart Heartbeat with triggers, Smart Context Management, Knowledge Graph (3-level nightly linking), SkillAdvisor (Dream Phase 3), SkillCreator
- **Phase 5 (done):** security hardening (sandbox, SSRF protection, audit, input sanitization), metrics, streaming polish, checkpoint, CI, `/set_access`, `/clone_agent`, file round-trip outbox, Skill Pool marketplace (install from community pool), Archivist agent (4th base) with SchemaAdvisor (Dream Phase 3b)
