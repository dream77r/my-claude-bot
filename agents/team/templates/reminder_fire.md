# Reminder Fire Template

Срабатывание напоминания, которое участник чата попросил поставить.

## Когда запускать

Из `schedule_check.md` для каждой записи в `reminders/{chat_id}.json`, где:
- `status == "pending"`
- `fire_at <= now_utc()`

## Что прочитать

1. `reminders/{chat_id}.json` — сама запись.
2. `groups/{chat_id}/settings.json` — `message_thread_id`, `language`.

## Шаблон сообщения

### Русский

```
⏰ @{to} — напоминание:

{text}

_(поставил @{created_by} в <дата создания>)_
```

### English

```
⏰ @{to} — reminder:

{text}

_(set by @{created_by} on <created_at>)_
```

## Действия

1. Сформируй сообщение по шаблону (язык из `settings.json`).
2. Если `to == "all"` — замени на «команда» и не тегай конкретно. Если `to` — конкретный username — тегни.
3. Запиши в `dispatch/{chat_id}_{thread_id}_{iso}.json` с `source: "reminder"` и `reminder_id: <id>`. Dispatcher опубликует сообщение в Telegram в течение ~5 секунд.
4. Обнови запись в `reminders/{chat_id}.json`:
   - Одноразовое (`repeat: null`): `status: "fired"`, `fired_at: <ISO>`.
   - Повторяющееся: вычисли следующий `fire_at`, оставь `status: "pending"`, инкремент `fire_count`.

## Вычисление следующего fire_at для повторов

- `repeat: "daily"` → `fire_at + 1 day`
- `repeat: "weekly:MON,TUE,WED,THU,FRI"` → следующий будний день в то же время
- `repeat: "weekly:MON"` → +7 дней
- `repeat: "cron:<expr>"` → распарсить cron-выражение и взять следующий момент

Всё — по таймзоне из `settings.json`.

## Ограничения

- Если `fire_at` старше 24 часов (напоминание «просроченное», мы его пропустили) — всё равно отправь, но добавь «(просрочено на N часов)».
- Если запись сломана (нет `text` или `to`) — пометь `status: "error"` и запиши в `log.md`.
- Максимум 10 срабатываний за один проход `schedule_check` (защита от флуда). Остальные — следующей минутой.
