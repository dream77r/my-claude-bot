"""My Claude Bot."""

import os
import shutil

# Путь к Claude CLI — определяется автоматически или через переменную окружения
_claude_cli_path: str | None = None


def get_claude_cli_path() -> str:
    """
    Найти путь к Claude CLI.

    Приоритет:
    1. Переменная окружения CLAUDE_CLI_PATH
    2. Поиск в PATH через shutil.which()
    3. Fallback: /usr/local/bin/claude
    """
    global _claude_cli_path
    if _claude_cli_path:
        return _claude_cli_path

    # 1. Env var
    env_path = os.environ.get("CLAUDE_CLI_PATH")
    if env_path:
        _claude_cli_path = env_path
        return _claude_cli_path

    # 2. Поиск в PATH
    found = shutil.which("claude")
    if found:
        _claude_cli_path = found
        return _claude_cli_path

    # 3. Fallback
    _claude_cli_path = "/usr/local/bin/claude"
    return _claude_cli_path
