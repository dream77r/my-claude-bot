"""
Sandbox — изоляция файловой системы для worker-агентов.

Master-агент имеет полный доступ к файловой системе.
Worker-агенты ограничены своей директорией памяти + /tmp.

Работает как PreToolUse CLI хук (тот же паттерн что command_guard):
- Проверяет пути в Read, Write, Edit, Glob, Grep
- Проверяет команды в Bash на пути за пределами sandbox
- Exit 0 = разрешить, Exit 2 = заблокировать

Конфиг в agent.yaml:
  sandbox:
    enabled: true          # default: true для worker, false для master
    allowed_paths:         # дополнительные разрешённые пути
      - "/tmp"
      - "/home/claude-agents/shared/"
"""

import json
import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Инструменты с file_path параметром
_FILE_TOOLS = {"Read", "Write", "Edit"}

# Инструменты с path/directory параметром
_PATH_TOOLS = {"Glob", "Grep"}

# Паттерны абсолютных путей в Bash-командах
_ABS_PATH_PATTERN = re.compile(r'(?:^|\s)(/[a-zA-Z][^\s"\']*)')

# Всегда разрешённые пути (системные, read-only)
_ALWAYS_ALLOWED = [
    "/tmp",
    "/usr/bin",
    "/usr/lib",
    "/bin",
    "/dev/null",
]


def is_path_allowed(
    path_str: str,
    sandbox_root: str,
    extra_allowed: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Проверить, находится ли путь внутри sandbox.

    Args:
        path_str: проверяемый путь
        sandbox_root: корень sandbox (директория агента)
        extra_allowed: дополнительные разрешённые пути

    Returns:
        (allowed, reason)
    """
    if not path_str:
        return True, "empty path"

    try:
        # Разрешить относительные пути (они внутри cwd = memory/)
        path = Path(path_str)
        if not path.is_absolute():
            return True, "relative path"

        resolved = path.resolve()
        sandbox_resolved = Path(sandbox_root).resolve()

        # Внутри sandbox?
        try:
            resolved.relative_to(sandbox_resolved)
            return True, "inside sandbox"
        except ValueError:
            pass

        # Внутри always-allowed?
        for allowed in _ALWAYS_ALLOWED:
            try:
                resolved.relative_to(Path(allowed).resolve())
                return True, f"system path: {allowed}"
            except ValueError:
                pass

        # Внутри extra_allowed?
        if extra_allowed:
            for allowed in extra_allowed:
                try:
                    resolved.relative_to(Path(allowed).resolve())
                    return True, f"allowed: {allowed}"
                except ValueError:
                    pass

        return False, f"за пределами sandbox: {resolved}"

    except Exception as e:
        return False, f"ошибка проверки пути: {e}"


def check_tool_sandbox(
    tool_name: str,
    tool_input: dict,
    sandbox_root: str,
    extra_allowed: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Проверить tool call на sandbox violation.

    Returns:
        (allowed, reason)
    """
    # File tools: Read, Write, Edit
    if tool_name in _FILE_TOOLS:
        file_path = tool_input.get("file_path", "")
        return is_path_allowed(file_path, sandbox_root, extra_allowed)

    # Path tools: Glob, Grep
    if tool_name in _PATH_TOOLS:
        path = tool_input.get("path", "")
        if path:
            return is_path_allowed(path, sandbox_root, extra_allowed)
        return True, "no path specified"

    # Bash: сканировать команду на абсолютные пути
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        for match in _ABS_PATH_PATTERN.finditer(command):
            abs_path = match.group(1)
            allowed, reason = is_path_allowed(
                abs_path, sandbox_root, extra_allowed
            )
            if not allowed:
                return False, f"команда содержит путь {reason}"
        return True, "ok"

    # Остальные инструменты — разрешить
    return True, "non-file tool"


def make_sandbox_hook(sandbox_root: str, extra_allowed: list[str] | None = None):
    """
    Создать on_tool_use хук для Hook-системы (логирование).

    Реальная блокировка — через PreToolUse CLI хук (main() ниже).
    """
    from .hooks import HookContext

    async def _sandbox_hook(ctx: HookContext) -> HookContext:
        tool_name = ctx.data.get("tool_name", "")
        tool_input = ctx.data.get("tool_input", {})

        allowed, reason = check_tool_sandbox(
            tool_name, tool_input, sandbox_root, extra_allowed
        )

        if not allowed:
            logger.warning(
                f"Sandbox violation от '{ctx.agent_name}': "
                f"{tool_name} — {reason}"
            )
            ctx.data["sandbox_blocked"] = True
            ctx.data["sandbox_reason"] = reason

        return ctx

    return _sandbox_hook


def main():
    """
    CLI точка входа для Claude Code PreToolUse хука.

    Вызывается как: python -m src.sandbox <sandbox_root> [extra_path1] [extra_path2]
    Читает CLAUDE_TOOL_USE_NAME и CLAUDE_TOOL_USE_INPUT из env.
    Exit 0 = разрешить, Exit 2 = заблокировать.
    """
    if len(sys.argv) < 2:
        # Без аргументов = sandbox отключён
        sys.exit(0)

    sandbox_root = sys.argv[1]
    extra_allowed = sys.argv[2:] if len(sys.argv) > 2 else None

    tool_name = os.environ.get("CLAUDE_TOOL_USE_NAME", "")
    tool_input_raw = os.environ.get("CLAUDE_TOOL_USE_INPUT", "{}")

    try:
        tool_input = json.loads(tool_input_raw)
    except json.JSONDecodeError:
        sys.exit(0)

    allowed, reason = check_tool_sandbox(
        tool_name, tool_input, sandbox_root, extra_allowed
    )

    if not allowed:
        print(
            f"🔒 Sandbox: доступ заблокирован — {reason}",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
