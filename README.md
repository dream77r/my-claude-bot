# My Claude Bot

[🇷🇺 Русская версия](README.ru.md)

Multi-agent Telegram platform powered by Claude Agent SDK. A fleet of AI agents with a shared message bus, background memory processing, cron jobs, and MCP integrations. Runs on a Claude Pro subscription ($20/mo, unlimited), not through the API.

## Features

- **Multi-agent fleet** -- multiple agents, each with its own Telegram bot, SOUL, and skills
- **MessageBus** -- async message bus between agents (pub/sub, broadcast, prefix routing)
- **Streaming responses** -- text appears in Telegram as it's generated, not as a single block
- **Dream Memory** -- background 2-phase memory processing on a schedule (fact extraction + wiki updates)
- **Heartbeat** -- proactive agent: checks tasks, executes them, decides whether to notify
- **Cron jobs** -- periodic tasks with cron expressions (digests, summaries, monitoring)
- **MCP servers** -- connect Todoist, GitHub, Google Calendar, and any MCP via config
- **Wiki memory** (Karpathy model) -- automatically records people, decisions, ideas with git versioning
- **Skills with dependencies** -- YAML frontmatter: checks commands and env vars before loading
- **Voice messages** -- transcription via Deepgram API (Nova-3)
- **Files** -- receive and analyze documents, photos via Telegram (up to 20MB)
- **Group chats** -- dual-mode (DM + groups), silent logging, isolated memory, topic support
- **Onboarding** -- language selection + profile setup on first launch
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

**Prerequisites:** Python 3.10+, Claude CLI (installed and authorized), Claude Pro subscription.

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

## Running with Docker (alternative)

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
- Claude Pro subscription
- Telegram bot (create via @BotFather)
- Deepgram API key (optional, for voice)
- Node.js/npx (optional, for MCP servers)

## Architecture

```
Telegram User → TelegramBridge → MessageBus → AgentWorker → Agent.call_claude()
                     ↑                                            ↓
                bus listener ← ── ── ── ── ── ── ← ── ── ── response/streaming
                     ↓
              StatusMessage (streaming, tool hints)

Background processes:
  Dream loop (every N hours) → Phase 1 (haiku: fact extraction)
                              → Phase 2 (sonnet: wiki updates)
  Heartbeat (every 30 min)   → Check → Execute → Evaluate → Notify?
  Cron (on schedule)         → Execute prompt → Notify
```

## Project Structure

```
src/
  main.py             -- entry point, fleet launcher
  agent.py            -- Agent: config, system prompt, Claude calls, MCP
  agent_worker.py     -- AgentWorker: bridges Agent with MessageBus
  telegram_bridge.py  -- Telegram handlers, message aggregation, streaming
  bus.py              -- MessageBus: pub/sub bus on asyncio.Queue
  orchestrator.py     -- message routing between agents
  dream.py            -- Dream Memory: background memory processing
  heartbeat.py        -- Heartbeat: proactive tasks
  cron.py             -- Cron: scheduled periodic tasks
  memory.py           -- Karpathy Wiki: profile, wiki/, daily notes, git-backed
  command_router.py   -- 4-level command router
  agent_manager.py    -- Agent Manager: create/list/validate agents
  cli.py              -- CLI interface for agent management
  tool_hints.py       -- tool status messages
  voice_handler.py    -- voice via Deepgram API
  file_handler.py     -- file upload/download

agents/me/                    -- strategic advisor
  agent.yaml                  -- config (bot_token, skills, dream, heartbeat, cron, mcp)
  SOUL.md                     -- agent personality
  skills/                     -- skills with YAML frontmatter
  templates/                  -- prompt templates for Dream
  memory/                     -- storage with git versioning

agents/coder/                 -- technical agent
  agent.yaml                  -- config (Bash, Edit, Grep and other dev-tools)
  SOUL.md                     -- coder personality
  skills/                     -- code-review, debugging
  templates/                  -- prompt templates for Dream
  memory/                     -- storage with git versioning

agents/team/                  -- team assistant (group chat)
  agent.yaml                  -- config (group access, research, task-tracking)
  SOUL.md                     -- Team Hub personality
  skills/                     -- task-tracking, knowledge-base, research
  templates/                  -- prompt templates for Dream
  memory/                     -- storage with git versioning
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome + onboarding on first launch |
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
| `/start_agent` | Start an agent by name |
| `/stop_agent` | Stop an agent by name |
| `/restart` | Restart the platform (applies code updates) |

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
- **Phase 2 (done):** MessageBus, Orchestrator, Dream Memory, Heartbeat, Skills frontmatter
- **Phase 3 (done):** multi-agent fleet, group chats, topics, streaming, MCP, cron, i18n
