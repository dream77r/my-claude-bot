#!/bin/sh
# Auto-detect Claude CLI binary from mounted versions directory.
# Solves two problems:
# 1. ~/.local/bin/claude is a symlink — Docker mounts it as empty dir
# 2. Version number changes on updates — no hardcoded paths needed

VERSIONS_DIR="/home/botuser/.local/share/claude/versions"

if [ -z "$CLAUDE_CLI_PATH" ] && [ -d "$VERSIONS_DIR" ]; then
    # Pick the newest version (by modification time)
    CLAUDE_BIN=$(ls -t "$VERSIONS_DIR"/* 2>/dev/null | head -1)
    if [ -n "$CLAUDE_BIN" ] && [ -x "$CLAUDE_BIN" ]; then
        export CLAUDE_CLI_PATH="$CLAUDE_BIN"
    fi
fi

exec "$@"
