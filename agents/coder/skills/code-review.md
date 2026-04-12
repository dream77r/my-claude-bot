---
name: code-review
version: 1.0.0
description: "Code review: анализ кода на баги, безопасность, качество"
license: MIT
when_to_use: "When user asks to review code, a file, or a diff"
triggers:
  keywords: ["ревью", "review", "проверь код", "проверь файл", "посмотри код", "diff", "качество кода", "pull request", " pr "]
  file_extensions: []
tags: [coding, review, quality]
requires_memory: []
requirements:
  commands: []
  env: []
always: true
---

# Skill: Code Review

## Когда активировать
Когда пользователь просит проверить код, файл или diff.

## Инструкции
1. Прочитай файл(ы) через Read
2. Проверь:
   - Баги и логические ошибки
   - Уязвимости (injection, XSS, секреты в коде)
   - Производительность (N+1, лишние аллокации)
   - Читаемость и именование
3. Группируй замечания по серьёзности: critical / warning / nit

## Формат ответа
```
**[файл:строка]** [critical/warning/nit] — описание

Исправление:
(блок кода)
```
