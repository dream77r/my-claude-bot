---
name: project-discovery
version: 1.0.0
description: "Поиск git-репозиториев на сервере и обновление реестра projects.md"
license: MIT
when_to_use: "When a task mentions a project not found in projects.md, or user asks to scan for projects"
triggers:
  keywords: ["найди проект", "не знаю где", "сканируй проекты", "добавь проект", "unknown project", "какие проекты есть"]
  file_extensions: []
tags: [git, discovery, projects]
requires_memory: [projects]
requirements:
  commands: ["find", "git"]
  env: []
always: false
---

# Skill: Project Discovery

## Когда активировать
Когда задача упоминает проект, которого нет в `projects.md`.
Или когда пользователь явно просит найти/добавить проект.

## Инструкции

### Шаг 1. Прочитай текущий реестр
```
Read: agents/coder/memory/projects.md
```
Если файла нет — создай из шаблона `agents/coder/templates/projects.md.example`.

### Шаг 2. Найди git-репозитории на сервере
```bash
find ~ /home -name ".git" -maxdepth 5 -type d 2>/dev/null \
  | sed 's|/.git$||' \
  | grep -v "/.git/" \
  | sort -u
```

### Шаг 3. Для каждого нового репозитория определи стек
```bash
# Путь к репо
cd /путь/к/репо

# Название и последний коммит
basename $(pwd) && git log -1 --format="%s"

# Стек
ls package.json requirements.txt Dockerfile go.mod Cargo.toml 2>/dev/null
head -5 README.md 2>/dev/null || true
```

### Шаг 4. Предложи пользователю
Покажи список найденных проектов которых нет в реестре:
```
Нашёл новые проекты на сервере:
• /home/user/my-api — Node.js (package.json)
• /home/user/scripts — Python (requirements.txt)

Добавить их в реестр?
```

### Шаг 5. Обнови projects.md
Если пользователь подтвердил — добавь записи в `agents/coder/memory/projects.md`.

## Формат записи
```markdown
## имя-репо
- **Путь:** /абсолютный/путь
- **Стек:** определённый стек
- **Описание:** из README или git log
- **Главные файлы:** автоопределённые ключевые файлы
```
