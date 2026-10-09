"""
Адаптер Xiaomi MiMo Code CLI (форк OpenCode).

Headless-режим: `mimo run --dangerously-skip-permissions "<prompt>"`.
stdout — текст транскрипта (строки с префиксом «• », thinking/прогресс — в
stderr). Формат вывода и флаги стоит сверить `mimo run --help` на хосте;
парсер текстовый и устойчив к мелким изменениям.

Права: для headless-запуска используем флаг skip-permissions (аналог
bypassPermissions у Claude). Тонкая настройка прав — через
.mimocode/mimocode.jsonc в рабочей директории агента.
"""

from __future__ import annotations

import logging
import os
import re

from .base import BackendSpec, OnTextDelta, OnToolUse
from .cli_common import compose_prompt, resolve_cli_path, run_cli

logger = logging.getLogger(__name__)

_BULLET_RE = re.compile(r"^•\s?")


class _MimoStream:
    def __init__(self, on_text_delta: OnTextDelta | None):
        self.lines: list[str] = []
        self.on_text_delta = on_text_delta

    async def on_line(self, line: str) -> None:
        text = _BULLET_RE.sub("", line).rstrip()
        if not text:
            return
        self.lines.append(text)
        if self.on_text_delta:
            try:
                await self.on_text_delta("\n".join(self.lines))
            except Exception:
                pass


class MimoCliBackend:
    spec = BackendSpec(
        id="mimo",
        display_name="MiMo Code",
        cli_command="mimo",
        cli_path_env="MIMO_CLI_PATH",
        default_model=None,
        models={},
        cheap_model=None,
        strong_model=None,
    )

    def _argv(self, prompt: str, model: str | None) -> list[str]:
        path = resolve_cli_path(self.spec.cli_command, self.spec.cli_path_env)
        argv = [path, "run", "--dangerously-skip-permissions"]
        if model:
            argv += ["--model", model]
        argv.append(prompt)
        return argv

    async def chat(
        self,
        *,
        message: str,
        system_prompt: str,
        cwd: str,
        model: str | None,
        on_text_delta: OnTextDelta | None = None,
        on_tool_use: OnToolUse | None = None,  # текстовый вывод — не парсим
        timeout: float = 1200.0,
    ) -> str:
        stream = _MimoStream(on_text_delta)
        await run_cli(
            argv=self._argv(compose_prompt(system_prompt, message), model),
            cwd=cwd,
            timeout=timeout,
            on_stdout_line=stream.on_line,
            display_name="mimo",
        )
        return "\n".join(stream.lines).strip()

    async def complete(
        self,
        *,
        prompt: str,
        model: str | None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        timeout: float = 600.0,
    ) -> str:
        stream = _MimoStream(None)
        await run_cli(
            argv=self._argv(compose_prompt(system_prompt, prompt), model),
            cwd=cwd or os.getcwd(),
            timeout=timeout,
            on_stdout_line=stream.on_line,
            display_name="mimo",
        )
        return "\n".join(stream.lines).strip()
