"""
Реестр LLM-бэкендов.

Список моделей — стартовый: CLI провайдеры принимают произвольные id моделей,
поэтому /model и валидация не ограничиваются жёстко этим списком. Перед
продом сверить доступные модели на хосте (`codex exec --help`, `kimi provider
catalog list`, `mimo models`).
"""

from __future__ import annotations

from .base import BackendSpec

BACKENDS: dict[str, BackendSpec] = {
    "claude": BackendSpec(
        id="claude",
        display_name="Claude Code",
        cli_command="claude",
        cli_path_env="CLAUDE_CLI_PATH",
        default_model="sonnet",
        models={
            "haiku": "Haiku — быстрая, дешёвая",
            "sonnet": "Sonnet — баланс скорости и качества",
            "opus": "Opus — максимальное качество",
        },
        cheap_model="haiku",
        strong_model="sonnet",
        supports_native_resume=True,
        supports_hooks=True,
        supports_mcp=True,
    ),
    "codex": BackendSpec(
        id="codex",
        display_name="OpenAI Codex",
        cli_command="codex",
        cli_path_env="CODEX_CLI_PATH",
        default_model="gpt-5.1-codex",
        models={
            "gpt-5.1-codex": "GPT-5.1 Codex — coding-агент",
            "gpt-5.1": "GPT-5.1 — универсальная",
            "gpt-5.1-codex-mini": "GPT-5.1 Codex Mini — дешёвая",
        },
        cheap_model="gpt-5.1-codex-mini",
        strong_model="gpt-5.1",
    ),
    "kimi": BackendSpec(
        id="kimi",
        display_name="Kimi Code",
        cli_command="kimi",
        cli_path_env="KIMI_CLI_PATH",
        default_model="kimi-code/kimi-for-coding",
        models={
            "kimi-code/kimi-for-coding": "Kimi for Coding — дефолт",
        },
        cheap_model="kimi-code/kimi-for-coding",
        strong_model="kimi-code/kimi-for-coding",
    ),
    "mimo": BackendSpec(
        id="mimo",
        display_name="MiMo Code",
        cli_command="mimo",
        cli_path_env="MIMO_CLI_PATH",
        default_model=None,  # дефолт платформы, -m не передаём
        models={},
        cheap_model=None,
        strong_model=None,
    ),
}


def get_spec(backend_id: str) -> BackendSpec:
    if backend_id not in BACKENDS:
        raise KeyError(f"Неизвестный бэкенд: {backend_id}")
    return BACKENDS[backend_id]
