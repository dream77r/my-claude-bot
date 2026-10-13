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
        default_model="gpt-6-luna",
        models={
            "gpt-6.1-sol": "GPT-6.1 Sol — код и длительные задачи",
            "gpt-6-astra": "GPT-6 Astra — сложные задачи",
            "gpt-6-sol": "GPT-6 Sol",
            "gpt-6-luna": "GPT-6 Luna — быстрые задачи",
            "gpt-5.6-sol": "GPT-5.6 Sol",
            "gpt-5.6-terra": "GPT-5.6 Terra",
            "gpt-5.6-luna": "GPT-5.6 Luna",
        },
        cheap_model="gpt-6-luna",
        strong_model="gpt-6.1-sol",
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
