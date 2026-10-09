"""
Адаптер OpenAI Codex CLI.

Headless-режим: `codex exec --json --sandbox <mode> --ask-for-approval never
[-s model] "<prompt>"`. stdout — JSONL-события (agent_message/delta,
turn.completed и т.п.). Флаги сверены с `codex exec --help` (v2025+); при
смене CLI — смотреть CliRunError со stderr.
"""

from __future__ import annotations

import json
import logging
import os

from .base import BackendSpec, OnTextDelta, OnToolUse
from .cli_common import (
    compose_prompt,
    extract_content_text,
    extract_tool_names,
    resolve_cli_path,
    run_cli,
)

logger = logging.getLogger(__name__)


def _event_text(obj: dict) -> str:
    msg = obj.get("message")
    if isinstance(msg, dict):
        text = extract_content_text(msg.get("content"))
        if text:
            return text
    for key in ("text", "delta", "content", "final_message"):
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return ""


class _CodexStream:
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

        text = _event_text(obj)
        if text:
            self.text += text + "\n"
            if self.on_text_delta:
                try:
                    await self.on_text_delta(self.text)
                except Exception:
                    pass


class CodexCliBackend:
    spec = BackendSpec(
        id="codex",
        display_name="OpenAI Codex",
        cli_command="codex",
        cli_path_env="CODEX_CLI_PATH",
        default_model="gpt-5.1-codex",
        models={},
        cheap_model="gpt-5.1-codex-mini",
        strong_model="gpt-5.1",
    )

    def _argv(self, prompt: str, model: str | None, sandbox: str) -> list[str]:
        path = resolve_cli_path(self.spec.cli_command, self.spec.cli_path_env)
        argv = [
            path, "exec",
            "--json",
            "--sandbox", sandbox,
            "--ask-for-approval", "never",
            "--skip-git-repo-check",
        ]
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
        on_tool_use: OnToolUse | None = None,
        timeout: float = 1200.0,
    ) -> str:
        stream = _CodexStream(on_text_delta, on_tool_use)
        await run_cli(
            argv=self._argv(
                compose_prompt(system_prompt, message), model, "workspace-write"
            ),
            cwd=cwd,
            timeout=timeout,
            on_stdout_line=stream.on_line,
            display_name="codex",
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
        # Внутренние джобы — read-only: пусть CLI сам ограничит, даже если
        # allowed_tools запрошены, но бэкенд не умеет их маппить.
        stream = _CodexStream(None, None)
        await run_cli(
            argv=self._argv(
                compose_prompt(system_prompt, prompt),
                model or self.spec.cheap_model,
                "read-only",
            ),
            cwd=cwd or os.getcwd(),
            timeout=timeout,
            on_stdout_line=stream.on_line,
            display_name="codex",
        )
        return stream.text.strip()
