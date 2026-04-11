# Dream Phase 2: Обновление технической wiki

Тебе даны извлечённые факты. Обнови файлы памяти.

## Извлечённые факты

{facts_json}

## Инструкции

- category "concept" → `wiki/concepts/{slug}.md` (архитектура, паттерны, решения)
- category "entity" → `wiki/entities/{slug}.md` (сервисы, репозитории, инструменты)
- category "profile" → обнови `profile.md`
- Обнови `index.md` для новых страниц

## Правила
- Минимальные точечные правки (Edit, не Write для существующих)
- Не дублируй информацию
- Slug: английский kebab-case (например: `docker-setup.md`, `auth-bug-fix.md`)
