#!/bin/bash
# One-shot bootstrap для серверов, где agent.yaml правился руками ДО того,
# как overlay-схема была отгружена. Старый update.sh ещё не знает про
# миграцию, git pull блокируется локальными правками — этот скрипт
# разруливает в один проход:
#
#   1. stash локальных правок в agents/
#   2. git pull --ff-only (подтянуть новый update.sh + scripts/)
#   3. stash pop; при конфликте — твоя версия побеждает (мы же её и хотели
#      сохранить)
#   4. ./update.sh — новая миграция увидит правки и создаст agent.local.yaml
#
# Запуск:
#   cd ~/my-claude-bot && \
#     curl -sSL https://raw.githubusercontent.com/dream77r/my-claude-bot/main/scripts/bootstrap_overlay.sh | bash
#
# Или локально, если репо уже обновлён:
#   bash scripts/bootstrap_overlay.sh

set -e

BOLD='\033[1m'
GREEN='\033[32m'
YELLOW='\033[33m'
RESET='\033[0m'

cd "$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "error: не в git-репозитории (cd ~/my-claude-bot и запусти снова)" >&2
    exit 1
}

echo -e "${BOLD}→ Stash локальных правок в agents/...${RESET}"
STASHED=0
if git diff --quiet -- agents/ 2>/dev/null && \
   [ -z "$(git ls-files --others --exclude-standard agents/)" ]; then
    echo "  (нечего stash'ить — working tree чистый в agents/)"
else
    git stash push -u -m "pre-overlay-bootstrap-$(date +%s)" -- agents/ >/dev/null
    STASHED=1
    echo -e "${GREEN}  ✓ stash сохранён${RESET}"
fi

echo ""
echo -e "${BOLD}→ git pull --ff-only...${RESET}"
git pull --ff-only
echo -e "${GREEN}  ✓ pulled${RESET}"

if [ "$STASHED" = "1" ]; then
    echo ""
    echo -e "${BOLD}→ Восстановление правок (твои побеждают при конфликте)...${RESET}"
    if git stash pop >/dev/null 2>&1; then
        echo -e "${GREEN}  ✓ stash pop чисто${RESET}"
    else
        # Конфликт при 3-way merge stash'а поверх обновлённого дерева.
        # Смысл bootstrap'а — СОХРАНИТЬ юзерские правки, чтобы миграция их
        # подобрала. Форсим версию из stash на конфликтных файлах и дропаем
        # stash (иначе он копится в списке).
        git checkout 'stash@{0}' -- agents/ 2>/dev/null || true
        # Индекс после conflict'а в состоянии "both modified" — сбросим.
        git reset HEAD agents/ >/dev/null 2>&1 || true
        git stash drop >/dev/null 2>&1 || true
        echo -e "${YELLOW}  ⚠ был конфликт — разрешён в пользу твоих правок${RESET}"
    fi
fi

echo ""
echo -e "${BOLD}→ ./update.sh (миграция подхватит правки)...${RESET}"
echo ""
exec ./update.sh
