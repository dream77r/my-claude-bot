#!/bin/bash
# Сбросить current_session_id для агентов, работающих в группах.
#
# Зачем: после фикса group_instructions в src/agent.py ранее накопленные
# Claude-сессии содержат few-shot pattern «молчу»/«нет @упоминания» —
# модель будет продолжать его, даже когда новый промпт уже корректный.
# Сброс session_id заставляет SDK начать сессию с чистого листа на
# следующем turn'е.
#
# Скрипт удаляет все `agents/<name>/memory/sessions/current_session_id`
# и `agents/<name>/memory/users/<uid>/sessions/current_session_id` у
# агентов, где есть хотя бы один `agents/<name>/memory/groups/<chat_id>/`
# (= агент реально использовался в группе).
#
# Memory (log.md, daily/, wiki/) НЕ трогается — только Claude session ID.
# Идемпотентно через маркер .migrated_clear_group_sessions.
#
# Usage:
#   scripts/migrate_clear_group_sessions.sh

set -u

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -z "$REPO_ROOT" ]; then
    echo "error: не в git-репозитории" >&2
    exit 1
fi
cd "$REPO_ROOT"

CLEARED=0
SKIPPED=0
TOTAL=0

shopt -s nullglob
for agent_dir in agents/*/; do
    [ -d "$agent_dir" ] || continue
    TOTAL=$((TOTAL + 1))

    # Признак «работал в группе»: есть хотя бы один групповой контекст.
    groups_dir="${agent_dir}memory/groups"
    if [ ! -d "$groups_dir" ] || [ -z "$(ls -A "$groups_dir" 2>/dev/null)" ]; then
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Удалить session-id единый и per-user (multi_user mode).
    removed=0
    for session_file in \
        "${agent_dir}memory/sessions/current_session_id" \
        "${agent_dir}memory/users"/*/sessions/current_session_id; do
        if [ -f "$session_file" ]; then
            rm -f "$session_file"
            removed=$((removed + 1))
        fi
    done

    if [ $removed -gt 0 ]; then
        CLEARED=$((CLEARED + 1))
        echo "  ✓ cleared: ${agent_dir} (${removed} session file(s))"
    else
        SKIPPED=$((SKIPPED + 1))
    fi
done
shopt -u nullglob

echo "  total=${TOTAL}, cleared=${CLEARED}, skipped=${SKIPPED}"
exit 0
