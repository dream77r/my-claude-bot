# Как запушить seed-контент в my-claude-bot-skills

Эта папка содержит **начальное содержимое** для публичного репозитория
[my-claude-bot-skills](https://github.com/dream77r/my-claude-bot-skills).

## Что внутри

- `README.md` — описание проекта и принципы
- `manifest.json` — каталог с одним скиллом
- `published/web-research.md` — первый опубликованный скилл
- `incoming/.gitkeep` — пустая папка карантина

## Инструкция

```bash
# 1. Клонируй свой пустой репо куда-нибудь временно
cd /tmp
git clone git@github.com:dream77r/my-claude-bot-skills.git
cd my-claude-bot-skills

# 2. Скопируй содержимое seed-папки
cp -r ~/my-claude-bot/scripts/skill-pool-seed/* ./
cp ~/my-claude-bot/scripts/skill-pool-seed/incoming/.gitkeep ./incoming/

# 3. Проверь что получилось
ls -la
cat manifest.json

# 4. Закоммить и запушь
git add .
git commit -m "Initial seed: web-research skill + manifest"
git push origin main
```

## Проверка что всё работает

После пуша:

```bash
# В my-claude-bot:
# 1. Задай SKILL_POOL_URL в .env
echo "SKILL_POOL_URL=https://github.com/dream77r/my-claude-bot-skills.git" >> .env

# 2. Обнови пул через CLI
python -m src.cli pool refresh
python -m src.cli pool list

# 3. Установи скилл тестовому агенту (например team)
python -m src.cli pool install web-research team

# 4. Проверь что файл появился
ls agents/team/skills/web-research.md
```

После этого перезапусти бота и попробуй в Telegram:
```
/poolskills
/installskill web-research @team
```

## Что дальше

Когда ты будешь публиковать новые скиллы (вручную или через будущий
publish-flow), они будут попадать сначала в `incoming/`, ты ревьюишь,
затем переносишь в `published/` и добавляешь запись в `manifest.json`.
