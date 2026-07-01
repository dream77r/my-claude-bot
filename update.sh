#!/bin/bash
# My Claude Bot — Update to latest version
# Usage: ./update.sh [-y|--yes]
#
# Flags:
#   -y, --yes   Non-interactive mode: auto-accept stash prompt,
#               auto-install bubblewrap if an agent requires it.
#               Also implied when stdin is not a TTY (CI, systemd timers,
#               remote ssh scripts).
#
# What it does:
# 1. Checks for local changes that might conflict (src/ + agents/ skills)
# 2. Pulls latest code from GitHub
# 3. Updates Python dependencies (always — idempotent)
# 4. Restarts the service (systemd or docker)
# 5. Shows what changed

set -e

# ══════════════════════════════════════════
# Флаги и интерактивность
# ══════════════════════════════════════════

AUTO_YES=0
STASHED_SKILLS=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) AUTO_YES=1 ;;
        -h|--help)
            sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown flag: $arg (try --help)"
            exit 2
            ;;
    esac
done

# Если stdin не TTY (CI, systemd ExecStart, ssh-скрипт), считаем что ответов
# не будет — включаем auto-yes, чтобы не зависнуть на `read -rp`.
if [ ! -t 0 ]; then
    AUTO_YES=1
fi

# Безопасный prompt: в интерактивном режиме читает ответ, в auto-yes —
# печатает выбранный default и возвращает его. Usage: _ask "текст" "Y" VAR_NAME
_ask() {
    local prompt_text="$1"
    local default_answer="$2"
    local out_var="$3"
    if [ "$AUTO_YES" -eq 1 ]; then
        printf "  %s [auto-%s]\n" "$prompt_text" "$default_answer"
        printf -v "$out_var" '%s' "$default_answer"
    else
        read -rp "  ${prompt_text} " "$out_var"
    fi
}

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

# Активировать pre-commit secret-guard (идемпотентно). Хук лежит в репо
# (scripts/git-hooks/) и не даёт случайно закоммитить .env или токен.
if [ -x scripts/git-hooks/pre-commit ] && \
   [ "$(git config --get core.hooksPath || true)" != "scripts/git-hooks" ]; then
    git config core.hooksPath scripts/git-hooks
fi

# ══════════════════════════════════════════
# Текущая версия
# ══════════════════════════════════════════

CURRENT_COMMIT=$(git rev-parse --short HEAD)
CURRENT_DATE=$(git log -1 --format="%ai" | cut -d' ' -f1)
echo -e "  Current version: ${CYAN}${CURRENT_COMMIT}${RESET} (${CURRENT_DATE})"

# ══════════════════════════════════════════
# Бэкап конфигов агентов (ДО любых изменений)
# ══════════════════════════════════════════
# agent.yaml/SOUL.md/skills — user data вне git. Снимаем перед pull/reset/
# миграциями, чтобы их нельзя было потерять при clean/reset. Не смертельно:
# сбой бэкапа не должен ронять апдейт (иначе set -e оборвёт скрипт).
if [ -x scripts/backup-configs.sh ]; then
    echo ""
    echo -e "${BOLD}Backing up agent configs...${RESET}"
    scripts/backup-configs.sh || echo -e "${YELLOW}  ⚠ config backup failed (continuing)${RESET}"
fi

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
    _ask "Continue? [Y/n]" "Y" CONFIRM
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

# ══════════════════════════════════════════
# Одноразовая миграция agent.yaml → agent.local.yaml
# ══════════════════════════════════════════
# Юзеры до overlay-схемы правили agents/*/agent.yaml руками (allowed_users,
# модель, prompt) — теперь такие правки блокируют git pull. Скрипт ниже
# разово переносит их в untracked agent.local.yaml и откатывает tracked
# файл к upstream. Маркер в корне репо (gitignored) — чтобы не гонять
# второй раз. Бежит только если маркера нет И origin/<branch> доступен.
if [ ! -f .migrated_config_overlay ] && [ -n "$REMOTE" ] && \
   [ -x scripts/migrate_agent_configs.sh ]; then
    echo ""
    echo -e "${BOLD}Migrating agent configs → overlay...${RESET}"
    if scripts/migrate_agent_configs.sh "$BRANCH"; then
        touch .migrated_config_overlay
        echo -e "${GREEN}  ✓ config overlay migration done${RESET}"
    else
        echo -e "${YELLOW}  ⚠ migration reported errors; попробую снова при следующем ./update.sh${RESET}"
    fi
fi

# ══════════════════════════════════════════
# Одноразовая миграция SOUL.md: убрать literal-following "молчи"
# ══════════════════════════════════════════
# В старом шаблоне team-агента была инструкция «В остальных случаях —
# молчи», которую LLM воспринимал буквально и слал слово «молчу» в чат
# вместо реального молчания. Скрипт находит SOUL.md с этим паттерном и
# заменяет блок на корректную формулировку (бридж сам фильтрует —
# агенту не нужно перепроверять @-упоминание). Идемпотентно через маркер.
if [ ! -f .migrated_soul_mute ] && [ -x scripts/migrate_soul_mute.sh ]; then
    echo ""
    echo -e "${BOLD}Migrating SOUL.md (mute literal-following fix)...${RESET}"
    if scripts/migrate_soul_mute.sh; then
        touch .migrated_soul_mute
        echo -e "${GREEN}  ✓ SOUL mute migration done${RESET}"
    else
        echo -e "${YELLOW}  ⚠ SOUL migration reported errors; попробую снова при следующем ./update.sh${RESET}"
    fi
fi

# ══════════════════════════════════════════
# Одноразовая миграция: сброс Claude session_id для групповых агентов
# ══════════════════════════════════════════
# Дополнение к фиксу literal-following: помимо текста промпта (SOUL/skill),
# был такой же паттерн «отвечай только когда обращаются» в hardcoded
# group_instructions (src/agent.py). После починки промпта старые сессии
# могут продолжать тащить few-shot «молчу». Сбрасываем session_id у всех
# агентов, у кого есть group context — следующий turn начнётся с чистого
# листа. Memory (log/wiki/daily) не трогается.
if [ ! -f .migrated_clear_group_sessions ] && \
   [ -x scripts/migrate_clear_group_sessions.sh ]; then
    echo ""
    echo -e "${BOLD}Clearing stale group session IDs...${RESET}"
    if scripts/migrate_clear_group_sessions.sh; then
        touch .migrated_clear_group_sessions
        echo -e "${GREEN}  ✓ group session reset done${RESET}"
    else
        echo -e "${YELLOW}  ⚠ session reset reported errors; попробую снова при следующем ./update.sh${RESET}"
    fi
fi

# ══════════════════════════════════════════
# Конфликты в agents/ (скиллы/SOUL — не стэшатся выше)
# ══════════════════════════════════════════
# Верхняя проверка (Checking for conflicts) стэшит только src/. Файлы в
# agents/*/skills/ и SOUL.md — user data, update.sh их не трогает. Но если
# локально правленый скилл ОБНОВЛЯЕТСЯ сверху, git pull --ff-only упадёт
# молча (set -e оборвёт скрипт без объяснения). Ловим заранее.
if [ -n "$REMOTE" ] && [ "$LOCAL" != "$REMOTE" ]; then
    _INCOMING_AGENTS=$(git diff --name-only "${LOCAL}..${REMOTE}" -- agents/ 2>/dev/null || true)
    _DIRTY_AGENTS=$(git diff --name-only -- agents/ 2>/dev/null || true)
    SKILL_CLASH=""
    if [ -n "$_INCOMING_AGENTS" ] && [ -n "$_DIRTY_AGENTS" ]; then
        SKILL_CLASH=$(printf '%s\n' "$_DIRTY_AGENTS" | grep -Fxf <(printf '%s\n' "$_INCOMING_AGENTS") 2>/dev/null || true)
    fi
    if [ -n "$SKILL_CLASH" ]; then
        echo ""
        echo -e "${YELLOW}  ⚠ Локальные правки в agents/-файлах, которые обновляются сверху:${RESET}"
        printf '%s\n' "$SKILL_CLASH" | while read -r f; do [ -n "$f" ] && echo -e "    ${DIM}$f${RESET}"; done || true
        echo -e "  ${DIM}По ним git pull --ff-only упадёт. Их можно застэшить и восстановить после.${RESET}"
        _ask "Stash эти правки и продолжить? [Y/n]" "Y" CONFIRM_SKILLS
        if [[ "$CONFIRM_SKILLS" =~ ^[nN] ]]; then
            echo -e "${YELLOW}  Update остановлен. Закоммить/застэшь правки в agents/ и запусти снова.${RESET}"
            [ "$STASHED" -eq 1 ] && git stash pop --quiet 2>/dev/null || true
            exit 0
        fi
        git stash push -m "pre-update-agents-$(date +%Y%m%d_%H%M%S)" -- agents/ >/dev/null
        STASHED_SKILLS=1
        echo -e "${GREEN}  ✓ Правки в agents/ застэшены${RESET}"
    fi
fi

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
        STASHED=0  # сброс: иначе финальный блок восстановления pop'нет
                   # повторно и вытащит ЧУЖОЙ старый стэш → ложный конфликт
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
fi

# ══════════════════════════════════════════
# Обновление зависимостей
# ══════════════════════════════════════════
# Гоним pip install безусловно — он идемпотентный и дешёвый (1-2с на no-op).
# Опора на `git diff LOCAL..HEAD` ломалась, когда оператор делал `git pull`
# руками до запуска скрипта: diff пуст, pip install пропускается, сервис
# падает на ModuleNotFoundError при новых зависимостях.

echo ""
echo -e "${BOLD}Syncing dependencies...${RESET}"
python3 -m pip install --user -q -r requirements.txt 2>&1 | tail -5
echo -e "${GREEN}  ✓ Dependencies in sync${RESET}"

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

# BUG_REPORT_CHAT_ID намеренно НЕ сидируется: канал — это решение оператора.
# Если ключ не задан в .env, скилл bugreport шлёт отчёт в личку владельцу
# (${BUG_REPORT_CHAT_ID:-$FOUNDER_TELEGRAM_ID}). Свой канал/группу можно
# прописать вручную (см. .env.example). Так публичные клоны не пишут в чужой чат.

if [ "$ENV_UPDATED" -eq 0 ]; then
    echo -e "${GREEN}  ✓ .env up to date${RESET}"
fi

# ══════════════════════════════════════════
# Восстановление stash
# ══════════════════════════════════════════

# Сначала agents/-стэш (он на вершине — пушился последним, после src/-стэша).
if [ "$STASHED_SKILLS" -eq 1 ]; then
    echo ""
    echo -e "${BOLD}Restoring your agents/ customizations...${RESET}"
    if git stash pop --quiet 2>/dev/null; then
        echo -e "${GREEN}  ✓ Правки в agents/ восстановлены${RESET}"
    else
        echo -e "${YELLOW}  ⚠ Конфликт при восстановлении — правки сохранены в git stash${RESET}"
        echo "  Разреши вручную: git stash list; git stash show -p | git apply"
    fi
fi

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
# Восстановление пропавших конфигов агентов (само-исцеление)
# ══════════════════════════════════════════
# Если git clean/reset/незапопленный stash стёр agent.yaml/SOUL/skills —
# доливаем их из последнего бэкапа. Только ОТСУТСТВУЮЩИЕ, existing не трогаем,
# так что это безопасно гонять при каждом апдейте. Агент без agent.yaml иначе
# молча не грузится — этот шаг закрывает главную причину «боты пропали».
if [ -x scripts/restore-configs.sh ]; then
    echo ""
    echo -e "${BOLD}Restoring any missing agent configs...${RESET}"
    scripts/restore-configs.sh || echo -e "${YELLOW}  ⚠ config restore skipped${RESET}"
fi

# ══════════════════════════════════════════
# Подсказка: skills:-override в agent.local.yaml
# ══════════════════════════════════════════
# overlay-merge заменяет списки целиком (src/agent.py::_deep_merge). Если в
# agent.local.yaml есть свой skills:, он перекрывает базовый — и скиллы,
# добавленные сверху в этом обновлении, НЕ зарегистрируются, пока их не
# дописать в локальный список. Чисто информируем (это не ошибка).
_LOCAL_SKILL_OVR=$(grep -rEl '^[[:space:]]*skills:' agents/*/agent.local.yaml 2>/dev/null || true)
if [ -n "$_LOCAL_SKILL_OVR" ]; then
    echo ""
    echo -e "${YELLOW}  ⚠ skills:-override в overlay — новые скиллы не подхватятся авто:${RESET}"
    printf '%s\n' "$_LOCAL_SKILL_OVR" | while read -r f; do [ -n "$f" ] && echo -e "    ${DIM}$f${RESET}"; done || true
    echo -e "  ${DIM}Сверь base agents/<имя>/agent.yaml и допиши недостающие скиллы в local-список.${RESET}"
fi

# ══════════════════════════════════════════
# Bubblewrap — проверка если хоть один агент его требует
# ══════════════════════════════════════════

# Простой grep по agents/*/agent.yaml на строку 'bubblewrap: true'.
# Не парсим YAML ради одного флага.
WANTS_BWRAP=$(grep -rEl '^\s*bubblewrap:\s*true' agents/*/agent.yaml 2>/dev/null || true)

if [ -n "$WANTS_BWRAP" ]; then
    echo ""
    echo -e "${BOLD}Checking bubblewrap (bash sandbox)...${RESET}"
    if command -v bwrap &>/dev/null; then
        echo -e "${GREEN}  ✓ bwrap installed${RESET}"
    elif command -v apt-get &>/dev/null; then
        echo -e "${YELLOW}  ⚠ Agents требуют bubblewrap, но bwrap не установлен${RESET}"
        AGENT_LIST=$(echo "$WANTS_BWRAP" | xargs -I{} dirname {} | xargs -I{} basename {} | tr '\n' ' ')
        echo -e "${DIM}    Агенты: ${AGENT_LIST}${RESET}"
        _ask "Install bubblewrap now (recommended)? [Y/n]" "Y" INSTALL_BWRAP
        if [[ ! "$INSTALL_BWRAP" =~ ^[nN] ]]; then
            if sudo apt-get install -y bubblewrap 2>&1 | tail -3; then
                echo -e "${GREEN}  ✓ bubblewrap installed${RESET}"
            else
                echo -e "${YELLOW}  ⚠ Install failed — bot будет работать с hook-only sandbox${RESET}"
            fi
        else
            echo -e "${YELLOW}  ○ Skipped — bot будет работать с hook-only sandbox${RESET}"
            echo -e "${DIM}    Чтобы поставить позже:  sudo apt-get install -y bubblewrap${RESET}"
        fi
    else
        echo -e "${YELLOW}  ⚠ bwrap не установлен, apt-get недоступен — hook-only sandbox${RESET}"
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
