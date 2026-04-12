---
name: knowledge-base
version: 1.0.0
description: "База знаний команды: хранение и поиск информации"
license: MIT
when_to_use: "When someone asks about past decisions, team knowledge, or shares important info to remember"
triggers:
  keywords: ["запомни", "помни", "что мы решили", "где у нас", "кто отвечает", "наше решение", "wiki", "запиши", "сохрани в базу", "база знаний", "мы договорились"]
  file_extensions: []
tags: [knowledge, team, memory, wiki]
requires_memory: []
requirements:
  commands: []
  env: []
always: true
---

# Skill: База знаний

## Когда активировать
Когда кто-то:
- Спрашивает "что мы решили по...", "где у нас...", "кто отвечает за..."
- Делится важной информацией, которую стоит запомнить
- Принимает решение в группе

## Инструкции
1. Для поиска — используй Grep по wiki/ и Read для конкретных файлов
2. Для записи — обнови или создай страницу в wiki/ через Write/Edit
3. Всегда обновляй index.md при создании новой страницы
4. Решения записывай в wiki/concepts/decisions.md с датой и контекстом

## Формат ответа
При поиске: краткий ответ + "Подробнее: wiki/concepts/название.md"
При записи: "📝 Записал в [файл]"
