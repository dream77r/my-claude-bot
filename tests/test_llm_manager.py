"""Тесты глобального выбора бэкенда (src.llm.manager).

Конфиг репозитория не трогаем: config_path патчится на tmp-файл,
кэш сбрасывается в setup/teardown каждого теста.
"""

import pytest
import yaml

from src.llm import manager
from src.llm.manager import BackendConfig


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """config_path → tmp (файла нет), кэш сброшен до и после теста."""
    monkeypatch.setattr(
        manager, "config_path", lambda: tmp_path / "backend.yaml"
    )
    monkeypatch.delenv("BOT_BACKEND", raising=False)
    manager.invalidate_cache()
    yield tmp_path
    manager.invalidate_cache()


def test_load_defaults_to_claude_without_config(isolated_config):
    cfg = manager.load()
    assert cfg.backend == "claude"
    assert cfg.model is None
    assert cfg.internal_model is None


def test_load_reads_config_file(isolated_config):
    (isolated_config / "backend.yaml").write_text(
        yaml.safe_dump(
            {"backend": "codex", "model": "gpt-5.1", "internal_model": "mini"}
        ),
        encoding="utf-8",
    )
    cfg = manager.load()
    assert cfg.backend == "codex"
    assert cfg.model == "gpt-5.1"
    assert cfg.internal_model == "mini"


def test_load_unknown_backend_in_file_falls_back_to_claude(isolated_config):
    (isolated_config / "backend.yaml").write_text(
        yaml.safe_dump({"backend": "not-a-backend"}), encoding="utf-8"
    )
    assert manager.load().backend == "claude"


def test_load_env_var_used_when_no_config_file(isolated_config, monkeypatch):
    monkeypatch.setenv("BOT_BACKEND", "kimi")
    assert manager.load().backend == "kimi"


def test_set_global_writes_yaml_and_load_returns_it(isolated_config):
    manager.set_global("codex", model="gpt-5.1")
    cfg = manager.load()
    assert cfg.backend == "codex"
    assert cfg.model == "gpt-5.1"
    # Файл на диске тоже записан
    data = yaml.safe_load(
        (isolated_config / "backend.yaml").read_text(encoding="utf-8")
    )
    assert data["backend"] == "codex"


def test_set_global_unknown_backend_raises(isolated_config):
    with pytest.raises(KeyError):
        manager.set_global("not-a-backend")
    # Ничего не записано
    assert not (isolated_config / "backend.yaml").exists()


def test_resolve_agent_backend_overrides_global(isolated_config):
    manager.set_global("claude")
    spec, model = manager.resolve(agent_backend="codex")
    assert spec.id == "codex"
    assert model == "gpt-5.1-codex"  # default_model бэкенда


def test_resolve_user_model_applies_only_when_in_models(isolated_config):
    manager.set_global("claude")
    spec, model = manager.resolve(
        agent_backend="codex", user_model="gpt-5.1"
    )
    assert model == "gpt-5.1"

    # Неизвестная модель игнорируется — падаем на дефолт бэкенда
    _, model = manager.resolve(agent_backend="codex", user_model="bogus")
    assert model == "gpt-5.1-codex"


def test_resolve_user_model_any_when_models_list_empty(isolated_config):
    manager.set_global("claude")
    spec, model = manager.resolve(agent_backend="mimo", user_model="whatever")
    assert spec.id == "mimo"
    assert model == "whatever"


def test_resolve_agent_backend_model_fallback(isolated_config):
    manager.set_global("claude")
    _, model = manager.resolve(
        agent_backend="codex", agent_backend_model="gpt-5.1-codex-mini"
    )
    assert model == "gpt-5.1-codex-mini"


def test_resolve_global_model_fallback(isolated_config):
    manager.set_global("codex", model="gpt-5.1")
    spec, model = manager.resolve()
    assert spec.id == "codex"
    assert model == "gpt-5.1"


def test_resolve_default_model_last_resort(isolated_config):
    manager.set_global("kimi")  # model=None в конфиге
    spec, model = manager.resolve()
    assert spec.id == "kimi"
    assert model == "kimi-code/kimi-for-coding"


def test_backend_config_is_frozen():
    cfg = BackendConfig(backend="claude")
    with pytest.raises(AttributeError):
        cfg.backend = "codex"
