---
name: bugreport
version: 1.1.0
description: "Сбор баг-репорта и отправка разработчику напрямую в Telegram"
license: MIT
when_to_use: "When an error, crash, or unexpected behavior is reported by user or detected in logs"
triggers:
  keywords: ["bugreport", "bug report", "баг репорт", "отправь баг", "сообщи об ошибке", "пришли логи", "ошибка в боте", "бот упал", "бот сломался", "report error", "submit bug"]
  file_extensions: []
tags: [debugging, support, report, logs]
requires_memory: []
requirements:
  commands: ["git", "curl"]
  env: ["ME_BOT_TOKEN", "FOUNDER_TELEGRAM_ID"]
always: false
---

# Skill: Bug Report

## Когда активировать
Когда пользователь сообщает об ошибке, или агент поймал исключение.

## Инструкции

### Шаг 1. Собери контекст

Выполни через Bash:

```bash
# Корень проекта — авто-детект из git, фоллбэк на $PWD (работает на любой инсталляции)
MCB_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

# Версия кода
echo "=== VERSION ===" && git -C "$MCB_DIR" rev-parse HEAD 2>/dev/null && git -C "$MCB_DIR" log -1 --format="%ai %s" 2>/dev/null

# Логи с ошибками (последние 150 строк)
echo "=== LOGS ===" && journalctl --user -u my-claude-bot -n 150 --no-pager 2>/dev/null || docker logs my-claude-bot --tail=150 2>/dev/null || echo "Логи недоступны"

# Системное состояние
echo "=== SYS ===" && hostname && python3 --version && uptime
```

### Шаг 2. Прочти конфиг

Прочитай `$MCB_DIR/agents/me/agent.yaml` (путь из шага 1) — включи в отчёт без значений переменных `${...}`.

### Шаг 3. Сформируй текст отчёта

Собери в переменную (не показывай пользователю до отправки):

```
🐛 Bug Report — me @ {hostname}

Версия: {git_commit} ({дата})
Время: {timestamp}

Описание от пользователя:
{что рассказал пользователь}

Ошибка / Traceback:
{полный traceback из логов}

Логи (последние строки перед ошибкой):
{20 строк}

agent.yaml (без токенов):
{конфиг}

Окружение: Python {версия}, uptime: {uptime}
```

### Шаг 4. Отправь через Telegram API

Выполни через Bash (подставь реальный текст отчёта в переменную REPORT):

```bash
# Загрузи переменные окружения (авто-детект корня — работает на любой инсталляции)
MCB_DIR="${MCB_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
set -a && source "$MCB_DIR/.env" && set +a

# Отправь отчёт
REPORT="🐛 Bug Report — me @ $(hostname)

Версия: $(git rev-parse --short HEAD 2>/dev/null) ($(git log -1 --format='%ai' 2>/dev/null))

[вставь собранный отчёт здесь]"

DEST="${BUG_REPORT_CHAT_ID:-$FOUNDER_TELEGRAM_ID}"

curl -s -X POST "https://api.telegram.org/bot${ME_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${DEST}" \
  --data-urlencode "text=${REPORT}" \
  -d "parse_mode=HTML" \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print('✅ Отправлено' if r.get('ok') else f'❌ Ошибка: {r}')"
```

`BUG_REPORT_CHAT_ID` — ID канала или группы для багов (если задан в `.env`).
Если не задан — репорт уходит напрямую в DM `FOUNDER_TELEGRAM_ID`.

### Шаг 5. Подтверди пользователю

После успешной отправки скажи:
> "Баг-репорт отправлен разработчику. Как только выйдет фикс — запусти `./update.sh`."

Если отправка не удалась — покажи отчёт прямо в чате с просьбой переслать вручную.
