"""
Абстракция LLM-бэкенда.

Каждый бэкенд — CLI-инструмент (claude, codex, kimi, mimo), у которого есть
headless-режим. Бэкенд умеет два вызова:
- chat() — полноценный ход диалога (streaming, инструменты, файлы);
- complete() — one-shot вызов для внутренних задач (heartbeat, cron, dream,
  consolidator, классификатор, knowledge graph).

Импорты конкретных SDK (claude_agent_sdk) — только внутри адаптеров, лениво:
бот должен работать без Claude CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

OnTextDelta = Callable[[str], Awaitable[None]]
OnToolUse = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class BackendSpec:
    """Описание бэкенда для реестра и админки."""

    id: str
    display_name: str
    cli_command: str                      # бинарь для shutil.which
    cli_path_env: str                     # env-override пути к бинарю
    default_model: str | None             # None → CLI сам решает (не передаём -m)
    models: dict[str, str] = field(default_factory=dict)  # id → описание (клавиатура)
    cheap_model: str | None = None        # внутренние джобы
    strong_model: str | None = None       # внутренние джобы посложнее
    supports_native_resume: bool = False  # CLI-native сессии (llm.history вместо этого)
    supports_hooks: bool = False          # claude-хуки (command_guard, sandbox.py)
    supports_mcp: bool = False            # MCP-серверы через SDK


class Backend(Protocol):
    """Экземпляр бэкенда. Реализуется адаптерами в этом пакете."""

    spec: BackendSpec

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
    ) -> str: ...

    async def complete(
        self,
        *,
        prompt: str,
        model: str | None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        timeout: float = 600.0,
    ) -> str: ...


class BackendError(RuntimeError):
    """Ошибка вызова бэкенда (CLI вернул ненулевой код, таймаут и т.п.)."""
