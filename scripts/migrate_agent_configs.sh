#!/bin/bash
# Миграция юзерских правок agent.yaml в overlay-схему.
#
# Для каждого agents/*/agent.yaml, который ОТЛИЧАЕТСЯ от upstream
# (origin/<branch>):
#   1. Выписать разницу в agents/<name>/agent.local.yaml через
#      scripts/extract_local_config.py.
#   2. Откатить agents/<name>/agent.yaml к upstream (чтобы git pull
#      больше не жаловался на локальные изменения).
#
# Безопасность:
#   - Ничего не делает, если agent.local.yaml уже существует.
#   - Если extract не удался — пропускает файл и идёт дальше (не падает).
#   - Требует git fetch origin заранее — сам его не вызывает.
#
# Usage:
#   scripts/migrate_agent_configs.sh [branch]  # default: main

set -u  # не падать от отсутствующих файлов (set -e) — каждый агент изолирован

BRANCH="${1:-main}"

# Путь к Python-хелперу рядом с этим скриптом (устойчиво к cwd).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXTRACT="$SCRIPT_DIR/extract_local_config.py"
if [ ! -f "$EXTRACT" ]; then
    echo "error: не нашёл $EXTRACT" >&2
    exit 1
fi

# Вычисляем корень репо — скрипт можно запускать откуда угодно.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -z "$REPO_ROOT" ]; then
    echo "error: не в git-репозитории" >&2
    exit 1
fi
cd "$REPO_ROOT"

# Проверка что origin/<branch> доступен — без fetch'а отличия увидеть нельзя.
if ! git rev-parse --verify --quiet "origin/${BRANCH}" >/dev/null; then
    echo "warn: origin/${BRANCH} недоступен — пропускаю миграцию" >&2
    exit 0
fi

MIGRATED=0
SKIPPED=0

# shopt не используем — простой glob работает стабильнее в bash 3.2+ на macOS.
for yaml in agents/*/agent.yaml; do
    # Если вообще нет совпадений, glob вернёт литерал — защита.
    [ -f "$yaml" ] || continue

    AGENT_NAME=$(basename "$(dirname "$yaml")")
    LOCAL_YAML="$(dirname "$yaml")/agent.local.yaml"

    # Уже есть overlay — миграция не нужна.
    if [ -f "$LOCAL_YAML" ]; then
        continue
    fi

    # Отличается ли working tree от upstream?
    # --quiet → exit 0 если нет разницы, 1 если есть. stderr гасим на случай
    # отсутствия файла в upstream (новый агент) — такой случай тоже skip.
    if git diff --quiet "origin/${BRANCH}" -- "$yaml" 2>/dev/null; then
        continue
    fi

    echo "migrate: $yaml → $LOCAL_YAML"

    # Извлекаем diff в overlay. Если скрипт вернул != 0 — пропускаем,
    # чтобы не рушить миграцию остальных агентов.
    if ! python3 "$EXTRACT" "$yaml" --branch "$BRANCH"; then
        echo "  warn: extract failed for $AGENT_NAME, skipping" >&2
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Если overlay не создался (skip-no-diff и т.п.) — агент не трогаем.
    if [ ! -f "$LOCAL_YAML" ]; then
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Откат agent.yaml к upstream — конфликт git pull исчезнет.
    if ! git checkout "origin/${BRANCH}" -- "$yaml" 2>/dev/null; then
        echo "  warn: git checkout failed for $yaml — overlay создан, но " \
             "файл не откачен; разрешите вручную" >&2
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    MIGRATED=$((MIGRATED + 1))
done

if [ "$MIGRATED" -gt 0 ] || [ "$SKIPPED" -gt 0 ]; then
    echo "config overlay migration: migrated=$MIGRATED skipped=$SKIPPED"
fi
