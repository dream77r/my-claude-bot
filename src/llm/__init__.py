"""
LLM-слой: единая точка вызова моделей для всего бота.

- registry — каталог бэкендов (claude/codex/kimi/mimo);
- manager — глобальный выбор (config/backend.yaml) и резолв на вызов;
- history — реплей истории диалога для бэкендов без нативного resume;
- адаптеры — конкретные CLI (claude_cli, codex_cli, kimi_cli, mimo_cli).
"""

from __future__ import annotations

from . import history, manager, registry
from .base import Backend, BackendError, BackendSpec
from .manager import get_backend, load, resolve, set_global

# Кликухи моделей из старых конфигов → тиры внутренних задач.
_TIER_ALIASES = {"haiku": "cheap", "sonnet": "strong", "opus": "strong"}


async def complete(
    *,
    prompt: str,
    model: str | None = None,
    system_prompt: str | None = None,
    cwd: str | None = None,
    allowed_tools: list[str] | None = None,
    timeout: float = 600.0,
    backend_id: str | None = None,
) -> str:
    """
    One-shot вызов для внутренних задач (heartbeat, cron, dream, consolidator,
    классификатор, knowledge graph) на активном (или указанном) бэкенде.

    model: legacy-алиасы ("haiku"/"sonnet"/"opus") маппятся на cheap/strong
    модели активного бэкенда; любая другая непустая строка пробрасывается
    как явный id модели. None → cheap (с override internal_model из конфига).
    """
    cfg = load()
    spec = registry.get_spec(backend_id or cfg.backend)
    inst = get_backend(spec)

    tier = _TIER_ALIASES.get(model or "")
    if tier == "cheap":
        resolved = cfg.internal_model or spec.cheap_model or spec.default_model
    elif tier == "strong":
        resolved = spec.strong_model or spec.default_model
    else:
        resolved = model or spec.cheap_model or spec.default_model

    return await inst.complete(
        prompt=prompt,
        model=resolved,
        system_prompt=system_prompt,
        cwd=cwd,
        allowed_tools=allowed_tools,
        timeout=timeout,
    )


__all__ = [
    "Backend",
    "BackendError",
    "BackendSpec",
    "complete",
    "get_backend",
    "history",
    "load",
    "manager",
    "registry",
    "resolve",
    "set_global",
]
