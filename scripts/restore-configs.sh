#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# restore-configs.sh — восстановить ОТСУТСТВУЮЩИЕ конфиги агентов из бэкапа.
#
# По умолчанию НЕ перезаписывает существующие файлы — доливает только те, что
# пропали (после git clean / reset / незапопленного stash). Так update.sh может
# само-исцелять флот, не затирая актуальные правки. Флаг --force перезаписывает
# всё из архива.
#
# Использование:
#   scripts/restore-configs.sh [archive.tar.gz] [--force]
# Без аргумента берёт latest:
#   $HOME/.my-claude-bot/config-backups/agent-configs_latest.tar.gz
#
# На НОВОМ сервере: скопируй сюда архив со старого, распакуй этим скриптом,
# пропиши токены в .env, перезапусти сервис — флот поднимется.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${MCB_CONFIG_BACKUP_DIR:-$HOME/.my-claude-bot/config-backups}"

ARCHIVE="$BACKUP_DIR/agent-configs_latest.tar.gz"
FORCE=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    -*) echo "unknown flag: $a" >&2; exit 2 ;;
    *) ARCHIVE="$a" ;;
  esac
done

if [ ! -f "$ARCHIVE" ]; then
  echo "  (no config backup at $ARCHIVE — skipping restore)"
  exit 0
fi

cd "$REPO_ROOT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$ARCHIVE" -C "$TMP"

restored=0
kept=0
while IFS= read -r f; do
  rel="${f#"$TMP"/}"
  if [ "$FORCE" -eq 0 ] && [ -e "$REPO_ROOT/$rel" ]; then
    kept=$((kept + 1))
    continue
  fi
  mkdir -p "$REPO_ROOT/$(dirname "$rel")"
  cp -p "$f" "$REPO_ROOT/$rel"
  echo "  + restored: $rel"
  restored=$((restored + 1))
done < <(find "$TMP" -type f)

if [ "$restored" -eq 0 ]; then
  echo "  ✓ all $kept config file(s) already present — nothing to restore"
else
  echo "  ✓ restored $restored missing config file(s); $kept already present"
fi
