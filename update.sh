#!/bin/bash
# My Claude Bot — Update to latest version
# Usage: ./update.sh
#
# What it does:
# 1. Checks for local changes that might conflict
# 2. Pulls latest code from GitHub
# 3. Updates Python dependencies
# 4. Restarts the service (systemd or docker)
# 5. Shows what changed

set -e

BOLD='\033[1m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
CYAN='\033[36m'
DIM='\033[2m'
RESET='\033[0m'

echo ""
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  My Claude Bot — Update                      ${RESET}"
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo ""

# ══════════════════════════════════════════
# Проверки
# ══════════════════════════════════════════

# Мы в правильной директории?
if [ ! -f "src/main.py" ]; then
    echo -e "${RED}Error: run this script from the my-claude-bot directory${RESET}"
    echo "  cd /path/to/my-claude-bot && ./update.sh"
    exit 1
fi

# Git доступен?
if ! command -v git &>/dev/null; then
    echo -e "${RED}Error: git not found${RESET}"
    exit 1
fi

# Это git-репозиторий?
if ! git rev-parse --git-dir &>/dev/null 2>&1; then
    echo -e "${RED}Error: not a git repository${RESET}"
    echo "  If you installed without git, re-install:"
    echo "  git clone https://github.com/dream77r/my-claude-bot.git"
    exit 1
fi

# ══════════════════════════════════════════
# Текущая версия
# ══════════════════════════════════════════

CURRENT_COMMIT=$(git rev-parse --short HEAD)
CURRENT_DATE=$(git log -1 --format="%ai" | cut -d' ' -f1)
echo -e "  Current version: ${CYAN}${CURRENT_COMMIT}${RESET} (${CURRENT_DATE})"

# ══════════════════════════════════════════
# Проверка локальных изменений в src/
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}Checking for conflicts...${RESET}"

# Проверяем изменения только в обновляемых файлах (не в agents/, .env, etc.)
LOCAL_CHANGES=$(git diff --name-only -- src/ templates/ requirements.txt setup.sh update.sh 2>/dev/null || true)

if [ -n "$LOCAL_CHANGES" ]; then
    echo -e "${YELLOW}  ⚠ You have local changes in updatable files:${RESET}"
    echo "$LOCAL_CHANGES" | while read -r f; do echo -e "    ${DIM}$f${RESET}"; done
    echo ""
    echo -e "  These will be stashed (saved) before update and can be restored."
    read -rp "  Continue? [Y/n] " CONFIRM
    if [[ "$CONFIRM" =~ ^[nN] ]]; then
        echo -e "${YELLOW}  Update cancelled.${RESET}"
        exit 0
    fi
    git stash push -m "pre-update-$(date +%Y%m%d_%H%M%S)" -- src/ templates/ requirements.txt setup.sh update.sh
    STASHED=1
    echo -e "${GREEN}  ✓ Changes stashed${RESET}"
else
    STASHED=0
    echo -e "${GREEN}  ✓ No conflicts${RESET}"
fi

# ══════════════════════════════════════════
# Обновление кода
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}Pulling latest code...${RESET}"

# Fetch и проверка — есть ли обновления
git fetch origin 2>/dev/null

BRANCH=$(git rev-parse --abbrev-ref HEAD)
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/${BRANCH}" 2>/dev/null || echo "")

if [ -z "$REMOTE" ]; then
    echo -e "${RED}  ✗ Cannot reach remote 'origin/${BRANCH}'${RESET}"
    echo "  Check your internet connection or remote URL:"
    echo "  git remote -v"
    if [ "$STASHED" -eq 1 ]; then
        git stash pop --quiet 2>/dev/null || true
    fi
    exit 1
fi

if [ "$LOCAL" = "$REMOTE" ]; then
    echo -e "${GREEN}  ✓ Code is up to date${RESET}"
    if [ "$STASHED" -eq 1 ]; then
        git stash pop --quiet
        echo -e "${GREEN}  ✓ Local changes restored${RESET}"
    fi
    # Не выходим — всё равно перезапустим сервис
    # (код мог быть обновлён через git pull до запуска скрипта)
    ALREADY_CURRENT=1
else
    ALREADY_CURRENT=0
fi

if [ "$ALREADY_CURRENT" -eq 0 ]; then
    # Показать что нового
    NEW_COMMITS=$(git log --oneline "${LOCAL}..${REMOTE}" 2>/dev/null)
    COMMIT_COUNT=$(echo "$NEW_COMMITS" | wc -l)

    echo -e "  ${CYAN}${COMMIT_COUNT} new commit(s):${RESET}"
    echo "$NEW_COMMITS" | head -15 | while read -r line; do echo -e "    ${DIM}${line}${RESET}"; done
    if [ "$COMMIT_COUNT" -gt 15 ]; then
        echo -e "    ${DIM}... and $((COMMIT_COUNT - 15)) more${RESET}"
    fi

    # Pull
    git pull --ff-only origin "$BRANCH" 2>/dev/null
    echo -e "${GREEN}  ✓ Code updated${RESET}"

    # ══════════════════════════════════════════
    # Обновление зависимостей
    # ══════════════════════════════════════════

    echo ""
    echo -e "${BOLD}Updating dependencies...${RESET}"

    # Проверяем, изменился ли requirements.txt
    REQ_CHANGED=$(git diff --name-only "${LOCAL}..HEAD" -- requirements.txt 2>/dev/null || true)

    if [ -n "$REQ_CHANGED" ]; then
        python3 -m pip install --user -q -r requirements.txt 2>&1 | tail -5
        echo -e "${GREEN}  ✓ Dependencies updated${RESET}"
    else
        echo -e "${GREEN}  ✓ No dependency changes${RESET}"
    fi
fi

# ══════════════════════════════════════════
# Миграция .env — добавить новые ключи
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}Checking .env migrations...${RESET}"

ENV_UPDATED=0

_add_env_key() {
    local KEY="$1"
    local VALUE="$2"
    local COMMENT="$3"
    if [ -f .env ] && ! grep -q "^${KEY}=" .env; then
        echo "" >> .env
        echo "# ${COMMENT}" >> .env
        echo "${KEY}=${VALUE}" >> .env
        echo -e "${GREEN}  ✓ Added ${KEY}${RESET}"
        ENV_UPDATED=1
    fi
}

_add_env_key "BUG_REPORT_CHAT_ID" "-1003998514795" "Канал для баг-репортов (@mcb-bugs)"

if [ "$ENV_UPDATED" -eq 0 ]; then
    echo -e "${GREEN}  ✓ .env up to date${RESET}"
fi

# ══════════════════════════════════════════
# Восстановление stash
# ══════════════════════════════════════════

if [ "$STASHED" -eq 1 ]; then
    echo ""
    echo -e "${BOLD}Restoring your local changes...${RESET}"
    if git stash pop --quiet 2>/dev/null; then
        echo -e "${GREEN}  ✓ Local changes restored${RESET}"
    else
        echo -e "${YELLOW}  ⚠ Merge conflict — your changes saved in git stash${RESET}"
        echo "  Resolve manually: git stash show -p | git apply"
    fi
fi

# ══════════════════════════════════════════
# Перезапуск сервиса
# ══════════════════════════════════════════

echo ""
echo -e "${BOLD}Restarting service...${RESET}"

RESTARTED=0

# Systemd (is-enabled проверяет что сервис настроен, даже если сейчас не запущен)
if systemctl --user is-enabled my-claude-bot &>/dev/null 2>&1; then
    systemctl --user restart my-claude-bot
    sleep 3
    if systemctl --user is-active my-claude-bot &>/dev/null; then
        echo -e "${GREEN}  ✓ Systemd service restarted${RESET}"
    else
        echo -e "${RED}  ✗ Service failed to start${RESET}"
        echo "  Check: journalctl --user -u my-claude-bot --no-pager -n 20"
    fi
    RESTARTED=1
fi

# Docker
if [ "$RESTARTED" -eq 0 ] && command -v docker &>/dev/null && docker compose ps 2>/dev/null | grep -q "my-claude-bot"; then
    docker compose up -d --build 2>&1 | tail -3
    echo -e "${GREEN}  ✓ Docker container rebuilt and restarted${RESET}"
    RESTARTED=1
fi

if [ "$RESTARTED" -eq 0 ]; then
    echo -e "${YELLOW}  ⚠ No running service found. Start manually:${RESET}"
    echo "    systemctl --user start my-claude-bot"
    echo "    # or: python3 -m src.main"
fi

# ══════════════════════════════════════════
# Итог
# ══════════════════════════════════════════

NEW_COMMIT=$(git rev-parse --short HEAD)
NEW_DATE=$(git log -1 --format="%ai" | cut -d' ' -f1)

echo ""
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  ✓ Updated!${RESET}"
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo ""
echo -e "  ${DIM}${CURRENT_COMMIT} (${CURRENT_DATE})${RESET} → ${CYAN}${NEW_COMMIT} (${NEW_DATE})${RESET}"
echo ""
