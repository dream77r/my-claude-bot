---
name: bugreport
version: 1.1.0
description: "Глубокий технический баг-репорт с автоотправкой разработчику в Telegram"
license: MIT
when_to_use: "When an error, crash, or unexpected behavior is reported. Coder agent does deep diagnosis."
triggers:
  keywords: ["bugreport", "bug report", "баг репорт", "отправь баг", "ошибка в боте", "бот упал", "сломалось", "traceback", "exception", "не работает", "report error"]
  file_extensions: [".log"]
tags: [debugging, support, report, logs, fix]
requires_memory: []
requirements:
  commands: ["git", "curl", "python3"]
  env: ["FOUNDER_TELEGRAM_ID"]
always: false
---

# Skill: Bug Report (Coder)

## Когда активировать
Когда пользователь сообщает об ошибке или когда поймано исключение.

## Инструкции

### Шаг 1. Полная диагностика

```bash
# Версия
git rev-parse HEAD && git log -1 --format="%ai %s" && git rev-parse --abbrev-ref HEAD

# Ошибки в логах
journalctl --user -u my-claude-bot -n 500 --no-pager 2>/dev/null \
  | grep -A 20 -B 3 "ERROR\|Traceback\|Exception\|CRITICAL" | head -200

# Ресурсы
free -h && df -h . && ps aux | grep python | grep -v grep

# Версии зависимостей
pip show anthropic python-telegram-bot 2>/dev/null | grep -E "^Name|^Version"
```

### Шаг 2. Найди код ошибки

По traceback найди файл:строку. Прочитай через Read (±15 строк вокруг ошибки).
Grep по ключевым словам из ошибки если traceback неполный.

### Шаг 3. Оцени — можно ли фикснуть локально

**Фиксишь сам (без разработчика):**
- `agents/*/agent.yaml` — опечатки в конфигах
- `agents/*/memory/` — битые файлы памяти
- Пропущенный ключ в `.env`

**Только разработчик:**
- `src/*.py`, `requirements.txt`, `Dockerfile`

### Шаг 4. Отправь технический отчёт

Загрузи `.env` и отправь через curl:

```bash
set -a && source .env && set +a

# Определи подходящий токен (coder или me)
BOT_TOKEN="${CODER_BOT_TOKEN:-$ME_BOT_TOKEN}"

COMMIT=$(git rev-parse --short HEAD 2>/dev/null)
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
HOST=$(hostname)
PYVER=$(python3 --version 2>&1)

# Текст отчёта — подставь реальные данные из шагов выше
REPORT="🐛 <b>Bug Report [CODER]</b> — ${HOST}

<b>Версия:</b> ${COMMIT} (${BRANCH})
<b>Python:</b> ${PYVER}

<b>Root cause (моя оценка):</b>
[заполни по итогам диагностики]

<b>Traceback:</b>
<pre>[вставь traceback]</pre>

<b>Код ([файл]:[строка]):</b>
<pre>[вставь релевантный код]</pre>

<b>Локальный фикс:</b>
[описание или N/A]

<b>Шаги воспроизведения:</b>
[что делал пользователь]"

DEST="${BUG_REPORT_CHAT_ID:-$FOUNDER_TELEGRAM_ID}"

curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d "chat_id=${DEST}" \
  --data-urlencode "text=${REPORT}" \
  -d "parse_mode=HTML" \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print('✅ Отправлено' if r.get('ok') else f'❌ Ошибка: {r}')"
```

### Шаг 5. Подтверди пользователю

> "Технический баг-репорт отправлен разработчику. После фикса запусти `./update.sh`."
