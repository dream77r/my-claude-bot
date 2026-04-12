# Morning Standup Template

Ты запускаешь утренний стендап для группы. Все переменные берутся из файлов памяти, ничего не хардкодь.

## Что прочитать перед началом

1. `groups/{chat_id}/settings.json` — `message_thread_id`, `language`, `daily_sync.morning_message_style`.
2. `wiki/entities/` — список участников (`Glob` + `Read` всех файлов).
3. `wiki/concepts/promises.md` — секция за вчерашнюю дату для переносов.

## Шаблон сообщения (адаптируй под `language`)

### Русский вариант

```
☀️ **Доброе утро, команда!**

{если есть вчерашние незакрытые: "Вчера остались:\n- @user1 — задача\n- @user2 — задача\n"}

Расскажите, что сегодня в работе?
{список тегов: @user1 @user2 @user3 ...}
```

### English вариант

```
☀️ **Good morning, team!**

{if yesterday's open items exist: "Carried over:\n- @user1 — task\n- @user2 — task\n"}

What's on your plate today?
{tag list: @user1 @user2 @user3 ...}
```

## Правила формирования

1. **Теги участников.** Бери `@username` из файла `wiki/entities/<username>.md` → поле `tg_username` во frontmatter. Если `tg_username` не заполнен, используй имя без тега.

2. **Фильтр участников.** Не тегай тех, у кого в entity-странице `frontmatter.active: false` или `frontmatter.away_until > сегодня`.

3. **Перенесённые задачи.** Бери из `promises.md` все `[ ]` из вчерашней секции, где нет строки `закрыто:`. Группируй по `@username`.

4. **Стиль.** Короткое, дружелюбное, без воды. Максимум 6 строк + теги.

## Действия после формирования

1. Запиши `dispatch/{chat_id}_{thread_id}_{iso_timestamp}.json`:
   ```json
   {
     "chat_id": <chat_id из settings>,
     "message_thread_id": <message_thread_id из settings, может быть null>,
     "text": "<сформированное сообщение>",
     "parse_mode": "Markdown",
     "source": "morning_standup"
   }
   ```
   Dispatcher-поллер автоматически опубликует это сообщение в Telegram и удалит файл.

2. Открой `wiki/synthesis/daily/YYYY-MM-DD.md` (создай если нет). Добавь:
   ```markdown
   ## Утренний стендап
   - время: ISO timestamp
   - chat_id: <id>
   - участников тегнуто: N
   - перенесено с вчера: M
   ```

3. Добавь строку в `log.md`: `[ISO] morning_standup: chat_id=X members=N carried=M`.

## Ограничения

- Если `wiki/entities/` пуст — не отправляй сообщение. Запиши в `log.md`: `morning_standup skipped: no members`.
- Если сегодня уже был стендап (секция уже есть в `wiki/synthesis/daily/YYYY-MM-DD.md`) — не дублируй.
- Не выдумывай задачи. Только то, что реально в `promises.md`.
