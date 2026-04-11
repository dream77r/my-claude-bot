"""
Command Guard — блокировка опасных команд.

Два режима работы:
1. Как on_tool_use хук в Hook-системе — логирование и уведомление
2. Как CLI-скрипт для Claude Code PreToolUse хука — реальная блокировка

Паттерн-матчинг по регулярным выражениям для предотвращения
деструктивных команд через Bash tool.
"""

import json
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)


# Опасные паттерны — регулярные выражения
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    # Filesystem destruction
    (r"rm\s+-rf\s+/(?!\w)", "rm -rf / — удаление корневой ФС"),
    (r"rm\s+-rf\s+~", "rm -rf ~ — удаление домашней директории"),
    (r"rm\s+-rf\s+\.\s*$", "rm -rf . — удаление текущей директории"),
    (r"mkfs\.", "mkfs — форматирование диска"),
    (r"dd\s+if=.*of=/dev/", "dd → /dev/ — перезапись диска"),
    (r">\s*/dev/sd", "перезапись блочного устройства"),

    # Fork bomb & system
    (r":\(\)\s*\{.*\|.*&", "fork bomb"),
    (r"chmod\s+-R\s+777\s+/(?!\w)", "chmod -R 777 / — открытие прав на всё"),

    # SQL destructive
    (r"DROP\s+(TABLE|DATABASE)", "DROP TABLE/DATABASE"),
    (r"TRUNCATE\s+TABLE", "TRUNCATE TABLE"),
    (r"DELETE\s+FROM\s+\w+\s*;", "DELETE без WHERE"),

    # Git destructive
    (r"git\s+push\s+.*--force", "git push --force"),
    (r"git\s+reset\s+--hard\s+(?!HEAD\b)", "git reset --hard (не HEAD)"),

    # Docker / system
    (r"docker\s+system\s+prune\s+-a", "docker system prune -a"),
]

# Скомпилированные паттерны
_COMPILED = [(re.compile(p, re.IGNORECASE), desc) for p, desc in DANGEROUS_PATTERNS]


def check_command(command: str) -> tuple[bool, str]:
    """
    Проверить команду на опасность.

    Args:
        command: строка команды из Bash tool

    Returns:
        (is_dangerous, description) — True если команда опасна
    """
    for pattern, desc in _COMPILED:
        if pattern.search(command):
            return True, desc
    return False, ""


def make_guard_hook(bus=None, agent_name: str = ""):
    """
    Создать on_tool_use хук для Hook-системы.

    При обнаружении опасной команды:
    - Логирует предупреждение
    - Отправляет уведомление пользователю через bus (если подключён)

    Заметка: этот хук НЕ блокирует выполнение — команда уже
    выполняется в Claude CLI. Для реальной блокировки используй
    PreToolUse хук (функция main() ниже).
    """
    from .hooks import HookContext

    async def _guard_hook(ctx: HookContext) -> HookContext:
        tool_name = ctx.data.get("tool_name", "")
        tool_input = ctx.data.get("tool_input", {})

        if tool_name != "Bash":
            return ctx

        command = tool_input.get("command", "")
        is_dangerous, desc = check_command(command)

        if is_dangerous:
            logger.warning(
                f"⚠️ Опасная команда от '{ctx.agent_name}': "
                f"{desc} — {command[:80]}"
            )
            ctx.data["guard_blocked"] = True
            ctx.data["guard_reason"] = desc

            # Уведомить через bus
            if bus:
                from .bus import FleetMessage, MessageType
                await bus.publish(FleetMessage(
                    source=f"guard:{ctx.agent_name}",
                    target=f"telegram:{agent_name or ctx.agent_name}",
                    content=f"⚠️ Обнаружена опасная команда: {desc}",
                    msg_type=MessageType.SYSTEM,
                    metadata={"event": "guard_warning", "command": command[:200]},
                ))

        return ctx

    return _guard_hook


def main():
    """
    CLI точка входа для Claude Code PreToolUse хука.

    Вызывается как: python -m src.command_guard
    Читает CLAUDE_TOOL_NAME и CLAUDE_TOOL_INPUT из env.
    Exit 0 = разрешить, Exit 2 = заблокировать.
    """
    tool_name = os.environ.get("CLAUDE_TOOL_USE_NAME", "")

    if tool_name != "Bash":
        sys.exit(0)

    tool_input_raw = os.environ.get("CLAUDE_TOOL_USE_INPUT", "{}")
    try:
        tool_input = json.loads(tool_input_raw)
    except json.JSONDecodeError:
        sys.exit(0)

    command = tool_input.get("command", "")
    is_dangerous, desc = check_command(command)

    if is_dangerous:
        print(
            f"⚠️ Заблокирована опасная команда: {desc}",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
