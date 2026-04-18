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

# Идемпотентность: если предыдущий запуск bootstrap'а умер на `git pull`,
# у нас в списке остался его stash. Вернём ВСЁ, что лежит в stash'ах
# с нашим маркером, чтобы сейчас застешить разом полное working tree.
OLD_STASHES=$(git stash list | grep -E "pre-overlay-bootstrap-" | cut -d: -f1 || true)
if [ -n "$OLD_STASHES" ]; then
    echo -e "${BOLD}→ Восстанавливаю stash'и от прошлой попытки...${RESET}"
    # Идём в обратном порядке (снизу) — так индексы не съезжают.
    echo "$OLD_STASHES" | tac | while read -r s; do
        # Конфликтов тут быть не должно — working tree пересекается только с
        # собственным старым stash'ем (с тех пор никто не pull'ил). Если
        # вдруг — переписываем из stash'а, чтобы не потерять правки.
        if ! git stash pop "$s" >/dev/null 2>&1; then
            CONFLICTS=$(git diff --name-only --diff-filter=U 2>/dev/null)
            if [ -n "$CONFLICTS" ]; then
                echo "$CONFLICTS" | while read -r f; do
                    git checkout "$s" -- "$f" 2>/dev/null || true
                done
                git reset HEAD -- $CONFLICTS >/dev/null 2>&1 || true
            fi
            git stash drop "$s" >/dev/null 2>&1 || true
        fi
    done
    echo -e "${GREEN}  ✓ прошлые stash'и вмержены${RESET}"
    echo ""
fi

echo -e "${BOLD}→ Stash локальных правок (весь working tree)...${RESET}"
# Стешим ВСЁ, не только agents/ — у юзеров бывают правки в src/, update.sh
# и т.п., которые тоже блокируют git pull. Конфликты при pop разрешаем в
# пользу юзера (см. ниже).
STASHED=0
if git diff --quiet && git diff --cached --quiet && \
   [ -z "$(git ls-files --others --exclude-standard)" ]; then
    echo "  (нечего stash'ить — working tree чистый)"
else
    git stash push -u -m "pre-overlay-bootstrap-$(date +%s)" >/dev/null
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
        # Смысл bootstrap'а — СОХРАНИТЬ юзерские правки (agent.yaml,
        # src/*, что бы там ни было), чтобы миграция и юзер после дальше
        # работали с ними. Находим конфликтные файлы и форсим stash'ную
        # версию по всему дереву.
        CONFLICTS=$(git diff --name-only --diff-filter=U 2>/dev/null)
        if [ -n "$CONFLICTS" ]; then
            echo "$CONFLICTS" | while read -r f; do
                git checkout 'stash@{0}' -- "$f" 2>/dev/null || true
            done
            git reset HEAD -- $CONFLICTS >/dev/null 2>&1 || true
            echo -e "${YELLOW}  ⚠ конфликт на: $(echo $CONFLICTS | tr '\n' ' ')${RESET}"
            echo -e "${YELLOW}    разрешено в пользу твоих правок${RESET}"
        fi
        git stash drop >/dev/null 2>&1 || true
    fi
fi

echo ""
echo -e "${BOLD}→ ./update.sh (миграция подхватит правки)...${RESET}"
echo ""
exec ./update.sh
