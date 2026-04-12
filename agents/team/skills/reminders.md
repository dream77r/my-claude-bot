---
name: reminders
version: 1.0.0
description: "Динамические напоминания от участников: приём, парсинг, хранение, срабатывание по расписанию"
license: MIT
when_to_use: "When a team member asks the bot to remind them about something at a specific time, or when cron triggers a scheduled reminder check"
triggers:
  keywords: ["напомни", "напоминай", "remind", "reminder", "через час", "через N минут", "завтра в", "в HH:MM", "не забыть"]
  file_extensions: []
tags: [reminders, cron, scheduling]
requires_memory: ["reminders/"]
requirements:
  commands: []
  env: []
always: false
---

# Skill: Reminders

## Когда активировать

1. Участник чата упомянул бота или сделал reply с просьбой вида «напомни X в Y», «не забудь напомнить мне через N минут», «завтра в 10 напомни про Z».
2. Cron-задача `schedule_check` обнаружила напоминание, чьё время сработало.

## Постановка напоминания

1. Извлеки из сообщения:
   - **кому** — по умолчанию отправитель (его tg username и user_id из контекста сообщения). Если сказано «напомни всем» — ставь `to: "all"`.
   - **что** — текст напоминания.
   - **когда** — ISO timestamp. Поддерживай:
     - «через N минут/часов» → `now() + N`
     - «в HH:MM» → сегодня в это время, если прошло — завтра
     - «завтра в HH:MM» → завтра
     - «в понедельник в HH:MM» / конкретная дата → распарси относительно текущей недели
     - Таймзону брать из `groups/{chat_id}/settings.json` → `timezone`. Если нет — UTC.
   - **повтор** — опционально: «каждый день», «по понедельникам», cron-выражение. Если нет — одноразовое.

2. Прочитай `reminders/{chat_id}.json` (если нет — создай как пустой массив).

3. Добавь запись:
   ```json
   {
     "id": "<uuid или timestamp>",
     "chat_id": <int>,
     "message_thread_id": <int_or_null>,
     "created_by": "<username>",
     "created_by_user_id": <int>,
     "to": "<username или all>",
     "text": "<что напомнить>",
     "fire_at": "<ISO timestamp>",
     "repeat": "<null | daily | weekly:MON,TUE | cron:...>",
     "status": "pending",
     "created_at": "<ISO timestamp>"
   }
   ```

4. Запиши файл через Write.

5. Кратко подтверди в чат: «⏰ Напомню @user в HH:MM: <кратко>».

## Срабатывание (из cron schedule_check)

1. Прочитай все `reminders/*.json`.
2. Для каждой записи со `status: pending` проверь: `fire_at <= now()`.
3. Если сработало:
   - Сформируй сообщение: «⏰ @<to>, напоминание: <text> (поставил @<created_by>)».
   - Запиши в `dispatch/{chat_id}_{thread_id}_{iso}.json` (формат как в `daily-sync`).
   - Если `repeat` пустой → поставь `status: "fired"` и `fired_at: <ISO>`.
   - Если `repeat` есть → вычисли следующий `fire_at` и оставь `status: pending`.
4. Сохрани файл через Write.

## Просмотр списка и отмена

Когда участник упоминает бота и просит «покажи мои напоминания», «отмени напоминание X», «убери все напоминания»:

1. Прочитай `reminders/{chat_id}.json`.
2. Отфильтруй по `created_by == запросивший` (или все, если админ группы — см. `settings.json` → `admins`).
3. Покажи список с номерами и краткими описаниями.
4. Для отмены — поменяй `status: "cancelled"`, добавь `cancelled_at` и `cancelled_by`.

## Ограничения

- Максимум 100 напоминаний на чат. Если больше — отказ + предложение удалить старые.
- `fire_at` не дальше 1 года вперёд.
- Не позволяй ставить напоминание другим участникам без их явного согласия (проверь, что `to` = отправитель, либо `to = "all"`).
- Если парсинг времени неуспешен — переспроси, не ставь наугад.
