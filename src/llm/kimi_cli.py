"""
Адаптер Kimi Code CLI.

Headless-режим: `kimi --prompt "<p>" --output-format stream-json [-m alias]`.
Каждая строка stdout — JSON-объект (assistant/tool сообщения). В -p-режиме
пермишены — auto (static deny правила CLI действуют). cwd = рабочая
директория сессии. Доки: https://www.kimi.com/code/docs/en/kimi-code-cli/
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .base import BackendSpec, OnTextDelta, OnToolUse
from .cli_common import (
    compose_prompt,
    extract_content_text,
    extract_tool_names,
    resolve_cli_path,
    run_cli,
)

logger = logging.getLogger(__name__)


def _extract_text(obj: dict) -> str:
    # Полное сообщение: {"message": {"role": "assistant", "content": ...}}
    msg = obj.get("message")
    if isinstance(msg, dict):
        text = extract_content_text(msg.get("content"))
        if text:
            return text
    # Дельта/плоский вариант
    for key in ("content", "text", "delta"):
        value = obj.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            text = extract_content_text(value.get("content"))
            if text:
                return text
    return ""


class _KimiStream:
    """Аккумулирует текст из stream-json и дёргает on_text_delta/on_tool_use."""

    def __init__(
        self,
        on_text_delta: OnTextDelta | None,
        on_tool_use: OnToolUse | None,
    ):
        self.text = ""
        self.on_text_delta = on_text_delta
        self.on_tool_use = on_tool_use

    async def on_line(self, line: str) -> None:
        line = line.strip()
        if not line or not line.startswith("{"):
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return

        if self.on_tool_use:
            for name in extract_tool_names(obj):
                try:
                    await self.on_tool_use(f"🔧 {name}")
                except Exception:
                    pass

        text = _extract_text(obj)
        if text:
            self.text += text + "\n"
            if self.on_text_delta:
                try:
                    await self.on_text_delta(self.text)
                except Exception:
                    pass


class KimiCliBackend:
    spec = BackendSpec(
        id="kimi",
        display_name="Kimi Code",
        cli_command="kimi",
        cli_path_env="KIMI_CLI_PATH",
        default_model="kimi-code/kimi-for-coding",
        models={},
        cheap_model="kimi-code/kimi-for-coding",
        strong_model="kimi-code/kimi-for-coding",
    )

    async def chat(
        self,
        *,
        message: str,
        system_prompt: str,
        cwd: str,
        model: str | None,
        on_text_delta: OnTextDelta | None = None,
        on_tool_use: OnToolUse | None = None,
        timeout: float = 1200.0,
    ) -> str:
        path = resolve_cli_path(self.spec.cli_command, self.spec.cli_path_env)
        argv = [path, "--prompt", compose_prompt(system_prompt, message),
                "--output-format", "stream-json"]
        if model:
            argv += ["--model", model]

        stream = _KimiStream(on_text_delta, on_tool_use)
        await run_cli(
            argv=argv,
            cwd=cwd,
            timeout=timeout,
            on_stdout_line=stream.on_line,
            display_name="kimi",
        )
        return stream.text.strip()

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
        path = resolve_cli_path(self.spec.cli_command, self.spec.cli_path_env)
        argv = [path, "--prompt", compose_prompt(system_prompt, prompt),
                "--output-format", "stream-json"]
        use_model = model or self.spec.cheap_model
        if use_model:
            argv += ["--model", use_model]

        stream = _KimiStream(None, None)
        await run_cli(
            argv=argv,
            cwd=cwd or os.getcwd(),
            timeout=timeout,
            on_stdout_line=stream.on_line,
            display_name="kimi",
        )
        return stream.text.strip()
