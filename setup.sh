#!/bin/bash
# My Claude Bot — Quick Setup
# Запуск: ./setup.sh
# Спрашивает токен и Telegram ID, собирает Docker, запускает бота.

set -e

BOLD='\033[1m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
RESET='\033[0m'

echo ""
echo -e "${BOLD}══════════════════════════════════════════${RESET}"
echo -e "${BOLD}  My Claude Bot — Быстрая настройка      ${RESET}"
echo -e "${BOLD}══════════════════════════════════════════${RESET}"
echo ""

# ── Проверки ──

echo -e "${BOLD}Проверяю окружение...${RESET}"

# Docker
if ! command -v docker &>/dev/null; then
    echo -e "${RED}  ✗ Docker не найден${RESET}"
    echo "    Установи: https://docs.docker.com/engine/install/"
    exit 1
fi
echo -e "${GREEN}  ✓ Docker${RESET}"

# docker compose
if ! docker compose version &>/dev/null 2>&1 && ! sudo docker compose version &>/dev/null 2>&1; then
    echo -e "${RED}  ✗ docker compose не найден${RESET}"
    echo "    Установи: https://docs.docker.com/compose/install/"
    exit 1
fi
echo -e "${GREEN}  ✓ Docker Compose${RESET}"

# Claude CLI
CLAUDE_PATH=$(which claude 2>/dev/null || true)
if [ -z "$CLAUDE_PATH" ]; then
    echo -e "${RED}  ✗ Claude CLI не найден${RESET}"
    echo "    Установи: https://docs.anthropic.com/en/docs/claude-code"
    exit 1
fi
echo -e "${GREEN}  ✓ Claude CLI${RESET}"

# Проверить авторизацию Claude CLI
if ! claude --version &>/dev/null 2>&1; then
    echo -e "${YELLOW}  ⚠ Claude CLI не авторизован${RESET}"
    echo "    Запусти: claude  и пройди авторизацию"
    exit 1
fi

# ── Шаг 1: Токен бота ──

echo ""
echo -e "${BOLD}Шаг 1: Токен Telegram-бота${RESET}"
echo ""
echo "  Если нет бота:"
echo "  1. Открой Telegram → @BotFather"
echo "  2. Отправь /newbot"
echo "  3. Скопируй токен (вида 123456:ABC-DEF...)"
echo ""

while true; do
    read -rp "  Токен бота: " BOT_TOKEN
    if [[ "$BOT_TOKEN" == *":"* ]] && [ ${#BOT_TOKEN} -gt 20 ]; then
        break
    fi
    echo -e "${RED}  Не похоже на токен. Формат: 123456:ABC-DEF...${RESET}"
done

# ── Шаг 2: Telegram ID ──

echo ""
echo -e "${BOLD}Шаг 2: Твой Telegram ID${RESET}"
echo ""
echo "  Как узнать:"
echo "  1. Открой Telegram → @userinfobot"
echo "  2. Отправь любое сообщение"
echo "  3. Он ответит твой ID (число)"
echo ""

while true; do
    read -rp "  Telegram ID: " TG_ID
    if [[ "$TG_ID" =~ ^[0-9]{5,}$ ]]; then
        break
    fi
    echo -e "${RED}  ID должен быть числом (минимум 5 цифр)${RESET}"
done

# ── Запись .env ──

HOST_HOME="$HOME"
cat > .env << EOF
# My Claude Bot
HOST_HOME=${HOST_HOME}
ME_BOT_TOKEN=${BOT_TOKEN}
FOUNDER_TELEGRAM_ID=${TG_ID}
EOF
chmod 600 .env
echo ""
echo -e "${GREEN}  ✓ .env создан${RESET}"

# ── Сборка и запуск ──

echo ""
echo -e "${BOLD}Собираю и запускаю Docker...${RESET}"
echo ""

sudo docker compose up -d --build

echo ""
echo -e "${BOLD}══════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  ✓ Бот запущен!${RESET}"
echo -e "${BOLD}══════════════════════════════════════════${RESET}"
echo ""
echo "  Открой Telegram и напиши боту любое сообщение."
echo "  Он проведёт онбординг: выбор языка и настройку профиля."
echo ""
echo -e "  ${YELLOW}Логи:      sudo docker compose logs -f${RESET}"
echo -e "  ${YELLOW}Перезапуск: sudo docker compose restart${RESET}"
echo -e "  ${YELLOW}Остановка:  sudo docker compose down${RESET}"
echo ""
