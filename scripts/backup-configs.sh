#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# backup-configs.sh — снимок конфигов агентов ВНЕ git.
#
# Per-agent config (agent.yaml, agent.local.yaml, SOUL.md, HEARTBEAT.md,
# settings.json, skills/) — это user data, которая живёт вне истории git
# (untracked или .gitignore'd). `git reset --hard`, `git clean -fd` или
# незапопленный `git stash -u` стирают её подчистую (оставляя только memory/),
# и агенты молча перестают грузиться: нет agent.yaml → нет бота.
#
# Скрипт складывает эти файлы в архив ВНЕ репозитория
# ($HOME/.my-claude-bot/config-backups по умолчанию) — clean/reset до него не
# дотягиваются. Архив портативен: скопируй на новый сервер, распакуй через
# restore-configs.sh — и флот поднимется (останется прописать токены в .env).
# memory/ НЕ бэкапится (runtime-данные, большие, со своим git-коммиттером).
#
# Env:
#   MCB_CONFIG_BACKUP_DIR   куда класть архивы (по умолч. ~/.my-claude-bot/config-backups)
#   MCB_CONFIG_BACKUP_KEEP  сколько таймстампованных архивов держать (по умолч. 10)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${MCB_CONFIG_BACKUP_DIR:-$HOME/.my-claude-bot/config-backups}"
KEEP="${MCB_CONFIG_BACKUP_KEEP:-10}"

cd "$REPO_ROOT"
[ -d agents ] || { echo "  (no agents/ dir — nothing to back up)"; exit 0; }

# Собираем конфиг-файлы + каталоги skills/, БЕЗ memory/ и *.bak.
mapfile -t ITEMS < <(
  find agents -maxdepth 2 -type f \
       \( -name 'agent.yaml' -o -name 'agent.local.yaml' -o -name 'SOUL.md' \
          -o -name 'HEARTBEAT.md' -o -name 'settings.json' \) 2>/dev/null
  find agents -maxdepth 2 -type d -name skills 2>/dev/null
)

if [ "${#ITEMS[@]}" -eq 0 ]; then
  echo "  (no agent config files found — nothing to back up)"
  exit 0
fi

mkdir -p "$BACKUP_DIR"
TS="$(date +%Y%m%d_%H%M%S)"
OUT="$BACKUP_DIR/agent-configs_${TS}.tar.gz"
tar --exclude='*.bak' -czf "$OUT" "${ITEMS[@]}"
cp -f "$OUT" "$BACKUP_DIR/agent-configs_latest.tar.gz"

N="$(tar -tzf "$OUT" | grep -c '' || true)"
echo "  ✓ configs backed up → $OUT ($(du -h "$OUT" | cut -f1), $N entries)"

# Ротация: держим последние $KEEP таймстампованных архивов (latest не трогаем).
ls -1t "$BACKUP_DIR"/agent-configs_2*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f || true
