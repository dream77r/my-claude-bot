"""Тесты реестра LLM-бэкендов (src.llm.registry)."""

import pytest

from src.llm import registry


def test_backends_contains_expected_ids():
    assert set(registry.BACKENDS) == {"claude", "codex", "kimi", "mimo"}


def test_backend_ids_unique_and_match_keys():
    ids = [spec.id for spec in registry.BACKENDS.values()]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(registry.BACKENDS)


def test_cli_command_and_env_set_for_all():
    for spec in registry.BACKENDS.values():
        assert isinstance(spec.cli_command, str) and spec.cli_command
        assert isinstance(spec.cli_path_env, str) and spec.cli_path_env


def test_model_tiers_are_consistent_with_models_list():
    """cheap/strong/default модели либо None, либо входят в список моделей
    (mimo списка не задаёт — CLI сам решает)."""
    for spec in registry.BACKENDS.values():
        for tier_model in (spec.cheap_model, spec.strong_model, spec.default_model):
            if tier_model is None:
                continue
            if spec.models:
                assert tier_model in spec.models, (
                    f"{spec.id}: модель '{tier_model}' вне spec.models"
                )


def test_only_claude_supports_hooks_mcp_native_resume():
    for spec in registry.BACKENDS.values():
        expected = spec.id == "claude"
        assert spec.supports_hooks is expected
        assert spec.supports_mcp is expected
        assert spec.supports_native_resume is expected


def test_get_spec_returns_spec():
    spec = registry.get_spec("codex")
    assert spec.id == "codex"
    assert spec.display_name


def test_get_spec_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        registry.get_spec("not-a-backend")
