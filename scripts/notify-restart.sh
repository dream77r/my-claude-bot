#!/bin/bash
# Отправить уведомление в Telegram при запуске бота.
# Вызывается из systemd ExecStartPost.

set -e

# Корень проекта — относительно скрипта, чтобы работало на любой инсталляции.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

# Прочитать токен и chat_id из .env
BOT_TOKEN=$(grep -E "^ME_BOT_TOKEN=" "$ENV_FILE" | cut -d= -f2)
CHAT_ID=$(grep -E "^FOUNDER_TELEGRAM_ID=" "$ENV_FILE" | cut -d= -f2)

if [ -z "$BOT_TOKEN" ] || [ -z "$CHAT_ID" ]; then
    exit 0  # Нет настроек — молча выходим
fi

# Собрать информацию
UPTIME=$(uptime -p 2>/dev/null || echo "N/A")
MEMORY=$(free -h | awk '/^Mem:/{print $3 "/" $2}')
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# Текст уведомления
TEXT="✅ Бот запущен
Время: $TIMESTAMP
Память сервера: $MEMORY
Сервер: $UPTIME"

# Отправить
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": ${CHAT_ID}, \"text\": \"${TEXT}\"}" \
    > /dev/null 2>&1 || true
