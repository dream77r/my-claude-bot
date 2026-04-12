---
name: debugging
version: 1.0.0
description: "Отладка: поиск и исправление багов"
license: MIT
when_to_use: "When user describes a bug, error, or unexpected behavior"
triggers:
  keywords: ["баг", "bug", "ошибк", "error", "не работает", "сломал", "падает", "exception", "traceback", "stacktrace", "краш", "crash", "исправ", "дебаг", "debug"]
  file_extensions: [".log", ".stacktrace"]
tags: [coding, debugging, troubleshooting]
requires_memory: []
requirements:
  commands: []
  env: []
always: true
---

# Skill: Debugging

## Когда активировать
Когда пользователь описывает баг, ошибку или неожиданное поведение.

## Инструкции
1. Попроси стектрейс или лог ошибки (если не приложен)
2. Найди релевантный код через Grep/Glob
3. Прочитай файл через Read
4. Определи root cause
5. Предложи fix с минимальным изменением
6. Если баг нетривиальный — запиши в wiki/concepts/bugs.md

## Формат ответа
```
**Root cause:** краткое описание

**Fix:**
(блок кода с исправлением)

**Почему:** объяснение в 1-2 предложения
```
