# my-claude-bot-skills

Публичный маркетплейс скиллов для [my-claude-bot](https://github.com/dream77r/my-claude-bot).

Совместим со стандартом [agentskills.io](https://agentskills.io) — скиллы отсюда
можно использовать с любым agentskills.io-совместимым агентом (Hermes, Claude,
OpenClaw и др.).

## Структура

```
├── manifest.json         Каталог метаданных всех published скиллов
├── published/            Готовые к установке скиллы (автоустановка из бота)
│   └── web-research.md
└── incoming/             Карантин — присланные пользователями, НЕ устанавливаются
                          автоматически. После ручного ревью переносятся в published/
```

## Как поставить скилл себе (из my-claude-bot)

1. Задай в `.env` своего бота:
   ```
   SKILL_POOL_URL=https://github.com/dream77r/my-claude-bot-skills.git
   ```
2. Перезапусти бота.
3. В Telegram: `/poolskills` — увидишь каталог.
4. Установить: `/installskill web-research`
5. Если скилл декларирует `requires_memory` — бот скажет какие файлы нужны.

## Принципы

- **Никаких личных данных в скилле.** Скилл это чистая методология: как
  действовать, куда ходить, какой формат ответа. Персональные данные (имена,
  цифры, FTP, дозировки лекарств) живут в `memory/` конкретного агента.
- **Строгая дисциплина публикации.** Если скилл содержит что-то личное — он
  не попадает в `published/`. Автоматический publish-flow будущих версий
  бота только переносит в `incoming/` — это карантин.
- **Совместимость с agentskills.io.** Каждый скилл это markdown-файл с YAML
  frontmatter: `name`, `version`, `description`, `license`, `when_to_use`,
  `triggers`, `tags`, `requires_memory`.

## Как предложить свой скилл

_Флоу публикации через бота — в разработке (Phase 4)._

Пока что — открой Pull Request с новым файлом в папке `incoming/`. Формат
файла см. существующие примеры в `published/`.

## Лицензия

Каждый скилл имеет своё поле `license` во frontmatter. По умолчанию — MIT.
