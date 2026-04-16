---
name: git-workflow
version: 1.0.0
description: "Git-коммит и пуш после изменений в проекте"
license: MIT
when_to_use: "After editing any files in a project — always commit and push the result"
triggers:
  keywords: ["закоммить", "commit", "запуши", "push", "сохрани изменения", "запиши в гит"]
  file_extensions: []
tags: [git, workflow, coding]
requires_memory: [projects]
requirements:
  commands: ["git"]
  env: []
always: true
---

# Skill: Git Workflow

## Когда активировать
После **любой** правки файлов в проекте — коммит обязателен.

## Инструкции

### Шаг 1. Проверь что изменилось
```bash
cd /путь/к/проекту
git status
git diff --stat
```

### Шаг 2. Добавь изменения
```bash
# Добавляй конкретные файлы, не git add -A
git add src/file.py другой/файл.py
```

### Шаг 3. Создай коммит
Формат сообщения: `тип: краткое описание на русском`

Типы:
- `feat:` — новая функциональность
- `fix:` — исправление бага
- `refactor:` — рефакторинг без изменения поведения
- `chore:` — настройки, зависимости, конфиги

```bash
git commit -m "feat: добавлен эндпоинт регистрации пользователей"
```

### Шаг 4. Запуши
```bash
git push
```

Если remote не настроен — пропусти пуш и сообщи об этом в отчёте.

### Шаг 5. Включи в отчёт
```
**Коммит:** abc1234 — feat: добавлен эндпоинт регистрации
```

## Важно
- Не используй `git add -A` — только конкретные файлы
- Не пуш `--force` без явного запроса
- Если есть незакоммиченные чужие изменения — сообщи, не трогай
