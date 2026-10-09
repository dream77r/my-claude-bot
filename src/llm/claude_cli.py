"""
Адаптер Claude Code (claude-agent-sdk).

Импорт claude_agent_sdk — строго ленивый (внутри функций): бот должен
стартовать и работать без Claude CLI. Полноценный чат-вызов с хуками,
sandbox и resume живёт в Agent._call_claude_sdk (там он связан с hooks/
consolidator/git_committer агента); этот модуль — complete() для внутренних
джоб и chat()-fallback общего вида.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import BackendSpec, OnTextDelta, OnToolUse
from .cli_common import resolve_cli_path

logger = logging.getLogger(__name__)


def sdk() -> Any:
    """Ленивый импорт claude_agent_sdk. ImportError — понятное сообщение."""
    try:
        import claude_agent_sdk
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "Бэкенд claude недоступен: пакет claude-agent-sdk не установлен. "
            "Переключитесь на другой бэкенд (/backend) или установите SDK."
        ) from e
    return claude_agent_sdk


class ClaudeCliBackend:
    spec = BackendSpec(
        id="claude",
        display_name="Claude Code",
        cli_command="claude",
        cli_path_env="CLAUDE_CLI_PATH",
        default_model="sonnet",
        models={},
        cheap_model="haiku",
        strong_model="sonnet",
        supports_native_resume=True,
        supports_hooks=True,
        supports_mcp=True,
    )

    def _cli_path(self) -> str:
        from .. import get_claude_cli_path

        return get_claude_cli_path() or resolve_cli_path(
            self.spec.cli_command, self.spec.cli_path_env
        )

    async def _run(
        self,
        *,
        prompt: str,
        model: str | None,
        system_prompt: str | None,
        cwd: str | None,
        allowed_tools: list[str] | None = None,
        on_text_delta: OnTextDelta | None,
        timeout: float,
    ) -> str:
        s = sdk()
        options = s.ClaudeAgentOptions(
            system_prompt=system_prompt,
            cwd=cwd,
            permission_mode="bypassPermissions",
            model=model or self.spec.default_model,
            cli_path=self._cli_path(),
        )
        if allowed_tools:
            options.allowed_tools = allowed_tools
        result_text = ""

        async def _pump():
            nonlocal result_text
            async for msg in s.query(prompt=prompt, options=options):
                if isinstance(msg, s.AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, s.TextBlock):
                            result_text += block.text
                            if on_text_delta:
                                try:
                                    await on_text_delta(result_text)
                                except Exception:
                                    pass
                elif isinstance(msg, s.ResultMessage):
                    if msg.result and not result_text:
                        result_text = msg.result

        await asyncio.wait_for(_pump(), timeout=timeout)
        return result_text.strip()

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
        # Fallback общего вида. Основной чат-путь Claude — в Agent._call_claude_sdk
        # (хуки, sandbox, resume); до него этот метод не доходит.
        return await self._run(
            prompt=message,
            model=model,
            system_prompt=system_prompt,
            cwd=cwd,
            on_text_delta=on_text_delta,
            timeout=timeout,
        )

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
        return await self._run(
            prompt=prompt,
            model=model or self.spec.cheap_model,
            system_prompt=system_prompt,
            cwd=cwd,
            allowed_tools=allowed_tools,
            on_text_delta=None,
            timeout=timeout,
        )
