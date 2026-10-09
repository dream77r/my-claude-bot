"""
Глобальный выбор бэкенда.

Конфиг — config/backend.yaml в корне репозитория. Резолвится в момент вызова,
поэтому переключение через /backend применяется ко всем агентам со следующего
хода, без перезапуска. Порядок дефолта: config/backend.yaml → env BOT_BACKEND
→ claude (для обратной совместимости; при отсутствии claude — первый
доступный CLI-бэкенд подсвечивается ошибкой вызова, см. адаптеры).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .base import Backend, BackendSpec
from .registry import BACKENDS, get_spec

CONFIG_RELATIVE = Path("config") / "backend.yaml"

DEFAULT_BACKEND = "claude"


@dataclass(frozen=True)
class BackendConfig:
    backend: str
    model: str | None = None          # None → дефолт бэкенда
    internal_model: str | None = None  # override cheap-модели для внутренних джоб


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    return project_root() / CONFIG_RELATIVE


_cache: tuple[float, BackendConfig] | None = None


def load() -> BackendConfig:
    """Прочитать глобальный конфиг (с кэшем по mtime)."""
    global _cache
    path = config_path()
    mtime = path.stat().st_mtime if path.exists() else 0.0
    if _cache and _cache[0] == mtime:
        return _cache[1]

    cfg = BackendConfig(backend=DEFAULT_BACKEND)
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            backend = str(data.get("backend") or DEFAULT_BACKEND)
            if backend not in BACKENDS:
                backend = DEFAULT_BACKEND
            cfg = BackendConfig(
                backend=backend,
                model=data.get("model") or None,
                internal_model=data.get("internal_model") or None,
            )
        except Exception:
            cfg = BackendConfig(backend=DEFAULT_BACKEND)
    elif os.environ.get("BOT_BACKEND") in BACKENDS:
        cfg = BackendConfig(backend=os.environ["BOT_BACKEND"])

    _cache = (mtime, cfg)
    return cfg


def set_global(
    backend: str,
    model: str | None = None,
    internal_model: str | None = None,
) -> BackendConfig:
    """Сохранить глобальный выбор бэкенда и применить его немедленно."""
    global _cache
    get_spec(backend)  # бросит KeyError на неизвестном id
    cfg = BackendConfig(backend=backend, model=model, internal_model=internal_model)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "backend": cfg.backend,
                "model": cfg.model,
                "internal_model": cfg.internal_model,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    _cache = (path.stat().st_mtime, cfg)
    return cfg


def resolve(
    *,
    agent_backend: str | None = None,
    agent_backend_model: str | None = None,
    user_model: str | None = None,
) -> tuple[BackendSpec, str | None]:
    """
    Определить активный бэкенд и модель для одного вызова.

    Бэкенд: agent.yaml → глобальный конфиг.
    Модель: per-user override (только если из списка моделей бэкенда; у бэкендов
    без списка — любая непустая) → agent.yaml backend_model → глобальный конфиг
    → дефолт бэкенда.
    """
    cfg = load()
    spec = get_spec(agent_backend or cfg.backend)

    model: str | None = None
    if user_model and (user_model in spec.models or not spec.models):
        model = user_model
    if model is None:
        model = agent_backend_model or cfg.model or spec.default_model
    return spec, model


_instances: dict[str, Backend] = {}


def get_backend(spec: BackendSpec) -> Backend:
    """Экземпляр адаптера по spec (кэшируется)."""
    if spec.id in _instances:
        return _instances[spec.id]
    if spec.id == "claude":
        from . import claude_cli

        inst: Backend = claude_cli.ClaudeCliBackend()
    elif spec.id == "codex":
        from . import codex_cli

        inst = codex_cli.CodexCliBackend()
    elif spec.id == "kimi":
        from . import kimi_cli

        inst = kimi_cli.KimiCliBackend()
    elif spec.id == "mimo":
        from . import mimo_cli

        inst = mimo_cli.MimoCliBackend()
    else:  # pragma: no cover — get_spec уже отсеял
        raise KeyError(f"Нет адаптера для бэкенда: {spec.id}")
    _instances[spec.id] = inst
    return inst


def invalidate_cache() -> None:
    """Сбросить кэш конфига (используется в тестах)."""
    global _cache
    _cache = None
