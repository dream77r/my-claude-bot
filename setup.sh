#!/bin/bash
# My Claude Bot — One-command setup
# Usage: git clone https://github.com/dream77r/my-claude-bot.git && cd my-claude-bot && ./setup.sh
#
# What it does:
# 1. Checks dependencies (Python, Claude CLI)
# 2. Asks for bot token and Telegram ID
# 3. Asks for resource limits (memory, CPU)
# 4. Creates .env, installs packages, sets up systemd
# 5. Starts the bot — ready to use in Telegram

set -e

BOLD='\033[1m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
CYAN='\033[36m'
RESET='\033[0m'

# ──────────────────────────────────────────────
# Install sudoers rule for /setup_dashboard.
# Idempotent: safe to call multiple times.
# ──────────────────────────────────────────────
install_dashboard_sudoers() {
    local script_dir helper sudoers tmp
    script_dir="$(cd "$(dirname "$0")" && pwd)"
    helper="${script_dir}/scripts/setup-dashboard.sh"
    sudoers="/etc/sudoers.d/my-claude-bot"

    if [ ! -x "$helper" ]; then
        echo -e "${RED}  ✗ Helper not found or not executable: ${helper}${RESET}"
        echo "    Make sure you're in the my-claude-bot repo and the script has +x."
        return 1
    fi

    echo -e "${BOLD}Installing sudoers rule for /setup_dashboard…${RESET}"
    echo "  Target: ${sudoers}"
    echo "  Allows: ${USER} → NOPASSWD: ${helper}"

    tmp="$(mktemp)"
    cat > "$tmp" <<EOF
# ${sudoers}
# Installed by ./setup.sh (lets the bot provision nginx + Let's Encrypt
# via /setup_dashboard). The helper itself validates domain/port/email
# before touching anything.

${USER} ALL=(root) NOPASSWD: ${helper}
EOF

    if ! sudo visudo -cf "$tmp" >/dev/null; then
        echo -e "${RED}  ✗ visudo validation failed, aborting.${RESET}"
        rm -f "$tmp"
        return 1
    fi

    sudo install -m 0440 -o root -g root "$tmp" "$sudoers"
    rm -f "$tmp"
    echo -e "${GREEN}  ✓ Sudoers rule installed.${RESET}"
    return 0
}

# Sub-command: --enable-dashboard (callable without full setup).
if [[ "${1:-}" == "--enable-dashboard" ]]; then
    if install_dashboard_sudoers; then
        echo ""
        echo -e "  Now open Telegram and run:"
        echo -e "    ${CYAN}/setup_dashboard <domain> [email]${RESET}"
        echo ""
        exit 0
    fi
    exit 1
fi

echo ""
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  My Claude Bot — Setup                       ${RESET}"
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo ""

# ══════════════════════════════════════════
# Проверки
# ══════════════════════════════════════════

echo -e "${BOLD}Checking dependencies...${RESET}"

# Python 3.10+
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}  ✗ Python 3 not found${RESET}"
    echo "    Install: sudo apt install python3 python3-pip"
    exit 1
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "${GREEN}  ✓ Python ${PY_VERSION}${RESET}"

# pip
if ! python3 -m pip --version &>/dev/null 2>&1; then
    echo -e "${RED}  ✗ pip not found${RESET}"
    echo "    Install: sudo apt install python3-pip"
    exit 1
fi
echo -e "${GREEN}  ✓ pip${RESET}"

# Claude CLI
CLAUDE_PATH=$(which claude 2>/dev/null || true)
if [ -z "$CLAUDE_PATH" ]; then
    echo -e "${RED}  ✗ Claude CLI not found${RESET}"
    echo "    Install: https://docs.anthropic.com/en/docs/claude-code"
    echo "    Then run: claude   (to authorize)"
    exit 1
fi
echo -e "${GREEN}  ✓ Claude CLI (${CLAUDE_PATH})${RESET}"

# git
if ! command -v git &>/dev/null; then
    echo -e "${RED}  ✗ git not found${RESET}"
    echo "    Install: sudo apt install git"
    exit 1
fi
echo -e "${GREEN}  ✓ git${RESET}"

# Node.js (optional, for qmd semantic search)
if command -v node &>/dev/null; then
    NODE_VERSION=$(node --version)
    echo -e "${GREEN}  ✓ Node.js ${NODE_VERSION} (for semantic search)${RESET}"
else
    echo -e "${YELLOW}  ○ Node.js not found (optional — for semantic wiki search)${RESET}"
    echo "    Install: sudo apt install nodejs npm"
fi

# systemd user session
if ! systemctl --user status &>/dev/null 2>&1; then
    echo -e "${YELLOW}  ⚠ systemd user session not available${RESET}"
    echo "    You may need to: loginctl enable-linger $USER"
    echo "    Or connect via SSH (not just su/sudo)"
fi

# ══════════════════════════════════════════
# Step 1: Bot token
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}Step 1: Telegram bot token${RESET}"
echo ""
echo -e "  ${CYAN}How to get:${RESET}"
echo "  1. Open Telegram → @BotFather"
echo "  2. Send /newbot"
echo "  3. Choose a name and username"
echo "  4. Copy the token (looks like: 123456789:ABC-DEF...)"
echo ""

while true; do
    read -rp "  Bot token: " BOT_TOKEN
    if [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] && [ ${#BOT_TOKEN} -gt 20 ]; then
        break
    fi
    echo -e "${RED}  Invalid token format. Example: 123456789:ABCdefGHI-jklMNO${RESET}"
done

# ══════════════════════════════════════════
# Step 2: Telegram ID
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}Step 2: Your Telegram user ID${RESET}"
echo ""
echo -e "  ${CYAN}How to find:${RESET}"
echo "  1. Open Telegram → search for @userinfobot"
echo "  2. Send it any message"
echo "  3. It replies with your ID (a number like 44117786)"
echo ""
echo "  This ID restricts bot access to you only."
echo ""

while true; do
    read -rp "  Telegram ID: " TG_ID
    if [[ "$TG_ID" =~ ^[0-9]{5,}$ ]]; then
        break
    fi
    echo -e "${RED}  ID must be a number (at least 5 digits)${RESET}"
done

# ══════════════════════════════════════════
# Step 3: Resource limits
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}Step 3: Resource limits${RESET}"
echo ""

# Show server resources
TOTAL_MEM=$(free -h | awk '/^Mem:/{print $2}')
TOTAL_CPU=$(nproc 2>/dev/null || echo "?")
echo -e "  ${CYAN}Your server: ${TOTAL_MEM} RAM, ${TOTAL_CPU} CPU cores${RESET}"
echo ""

# Memory
echo "  Max memory for the bot (examples: 512M, 1G, 2G)"
echo -e "  ${CYAN}Recommended: 512M for 1 agent, 1G for 3+ agents${RESET}"
read -rp "  Memory limit [1G]: " MEM_LIMIT
MEM_LIMIT=${MEM_LIMIT:-1G}

# CPU
echo ""
echo "  Max CPU cores (examples: 1, 2, 4)"
echo -e "  ${CYAN}Recommended: 1 for small servers, 2 for comfortable use${RESET}"
read -rp "  CPU cores [2]: " CPU_LIMIT
CPU_LIMIT=${CPU_LIMIT:-2}

# Compute MemoryHigh (80% of MemoryMax)
# Extract number and unit for MemoryHigh calculation
MEM_HIGH="${MEM_LIMIT}"
if [[ "$MEM_LIMIT" =~ ^([0-9]+)G$ ]]; then
    MEM_NUM=${BASH_REMATCH[1]}
    MEM_HIGH_NUM=$(( MEM_NUM * 800 ))
    MEM_HIGH="${MEM_HIGH_NUM}M"
elif [[ "$MEM_LIMIT" =~ ^([0-9]+)M$ ]]; then
    MEM_NUM=${BASH_REMATCH[1]}
    MEM_HIGH_NUM=$(( MEM_NUM * 80 / 100 ))
    MEM_HIGH="${MEM_HIGH_NUM}M"
fi

# ══════════════════════════════════════════
# Install dependencies
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}Installing Python packages...${RESET}"
python3 -m pip install --user -q claude-agent-sdk python-telegram-bot python-dotenv pyyaml httpx 2>&1 | tail -3
echo -e "${GREEN}  ✓ Python packages installed${RESET}"

# Очистка qmd от предыдущих версий (если был установлен)
if [ -e "$HOME/.local/bin/qmd" ] || [ -d "$HOME/.cache/qmd" ]; then
    echo -e "${BOLD}Cleaning up qmd (replaced by built-in wiki search)...${RESET}"
    rm -f "$HOME/.local/bin/qmd"
    rm -rf "$HOME/.local/lib/node_modules/@tobilu/qmd"
    rm -rf "$HOME/.cache/qmd"
    rmdir "$HOME/.local/lib/node_modules/@tobilu" 2>/dev/null || true
    echo -e "${GREEN}  ✓ qmd removed (wiki search is now built-in, no extra deps)${RESET}"
fi

# ══════════════════════════════════════════
# Create .env
# ══════════════════════════════════════════

cat > .env << EOF
# My Claude Bot
ME_BOT_TOKEN=${BOT_TOKEN}
FOUNDER_TELEGRAM_ID=${TG_ID}
EOF
chmod 600 .env
echo -e "${GREEN}  ✓ .env created (permissions: 600)${RESET}"

# ══════════════════════════════════════════
# Setup systemd user service
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}Setting up systemd service...${RESET}"

PROJECT_DIR="$(pwd)"
USER_HOME="$HOME"

mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/my-claude-bot.service << EOF
[Unit]
Description=My Claude Bot — Telegram multi-agent platform
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=/usr/bin/python3 -m src.main
Restart=always
RestartSec=5
Environment=PATH=${USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin

# Resource limits
MemoryMax=${MEM_LIMIT}
MemoryHigh=${MEM_HIGH}
CPUQuota=${CPU_LIMIT}00%
TasksMax=100

# Notification on (re)start
ExecStartPost=${PROJECT_DIR}/scripts/notify-restart.sh

[Install]
WantedBy=default.target
EOF

# Enable linger (keep services running after logout)
sudo loginctl enable-linger "$USER" 2>/dev/null || true

# Reload and start
systemctl --user daemon-reload
systemctl --user enable my-claude-bot
systemctl --user start my-claude-bot

echo -e "${GREEN}  ✓ Service started${RESET}"

# Wait for startup
sleep 3

# Check status
if systemctl --user is-active my-claude-bot &>/dev/null; then
    STATUS="${GREEN}running${RESET}"
else
    STATUS="${RED}failed (check: journalctl --user -u my-claude-bot)${RESET}"
fi

# ══════════════════════════════════════════
# Optional: enable /setup_dashboard from Telegram
# ══════════════════════════════════════════
#
# With this enabled, you can provision the web dashboard (nginx + Let's
# Encrypt + Mini App menu button) entirely from Telegram:
#     /setup_dashboard <your-domain>
# — no more terminal. Requires nginx + certbot installed and a domain
# whose A-record already points at this server.

SKIP_DASHBOARD_PROMPT="${SKIP_DASHBOARD_PROMPT:-}"
if [ -z "$SKIP_DASHBOARD_PROMPT" ] && [ -f /etc/sudoers.d/my-claude-bot ]; then
    SKIP_DASHBOARD_PROMPT=1  # already installed — skip silently
fi

if [ -z "$SKIP_DASHBOARD_PROMPT" ]; then
    echo ""
    echo -e "${BOLD}Enable /setup_dashboard from Telegram?${RESET}"
    echo -e "  ${CYAN}Lets you configure the web dashboard (domain + HTTPS + Mini App${RESET}"
    echo -e "  ${CYAN}menu button) by sending /setup_dashboard <domain> in Telegram.${RESET}"
    echo -e "  ${CYAN}You can do this later with: ./setup.sh --enable-dashboard${RESET}"
    echo ""
    read -rp "  Enable now? [Y/n]: " ENABLE_DASH
    ENABLE_DASH=${ENABLE_DASH:-Y}
    if [[ "$ENABLE_DASH" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        echo ""
        if ! install_dashboard_sudoers; then
            echo -e "${YELLOW}  ⚠ Could not install sudoers rule. Skip — you can retry later${RESET}"
            echo -e "${YELLOW}    with: ./setup.sh --enable-dashboard${RESET}"
        fi
    else
        echo -e "  ${CYAN}Skipped. Run ./setup.sh --enable-dashboard anytime.${RESET}"
    fi
fi

# ══════════════════════════════════════════
# Done
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  ✓ Bot is ready!${RESET}"
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Status: ${STATUS}"
echo -e "  Memory limit: ${MEM_LIMIT}"
echo -e "  CPU limit: ${CPU_LIMIT} cores"
echo ""
echo -e "  ${CYAN}Open Telegram and message your bot.${RESET}"
echo "  It will guide you through onboarding."
echo ""
echo -e "  ${BOLD}Useful commands:${RESET}"
echo "  Status:   systemctl --user status my-claude-bot"
echo "  Logs:     journalctl --user -u my-claude-bot -f"
echo "  Restart:  systemctl --user restart my-claude-bot"
echo "  Stop:     systemctl --user stop my-claude-bot"
echo "  Update:   cd ~/my-claude-bot && git pull && ./update.sh"
echo ""
echo "  Or use /restart directly in Telegram!"
echo ""
echo -e "  ${BOLD}Dashboard / Mini App:${RESET}"
echo "  Point a domain's A-record at this server, then send:"
echo -e "    ${CYAN}/setup_dashboard <your-domain>${RESET}  (in the master bot)"
echo ""
