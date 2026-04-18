"""Tests for agent.yaml + agent.local.yaml overlay (src/agent.py)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from src.agent import Agent, _deep_merge


# ── _deep_merge: чистая функция, без IO ──


class TestDeepMerge:
    def test_dict_recursive(self):
        base = {"a": {"b": 1, "c": 2}}
        overlay = {"a": {"c": 99, "d": 3}}
        assert _deep_merge(base, overlay) == {"a": {"b": 1, "c": 99, "d": 3}}

    def test_list_replaces(self):
        # Список в overlay полностью заменяет базовый (не конкатенация) —
        # иначе юзер не сможет СУЗИТЬ allowed_users, только расширить.
        base = {"x": [1, 2, 3]}
        overlay = {"x": [9]}
        assert _deep_merge(base, overlay) == {"x": [9]}

    def test_scalar_replaces(self):
        base = {"model": "sonnet"}
        overlay = {"model": "opus"}
        assert _deep_merge(base, overlay) == {"model": "opus"}

    def test_overlay_adds_new_key(self):
        base = {"a": 1}
        overlay = {"b": 2}
        assert _deep_merge(base, overlay) == {"a": 1, "b": 2}

    def test_empty_overlay_returns_base_copy(self):
        base = {"a": 1, "nested": {"b": 2}}
        result = _deep_merge(base, {})
        assert result == base
        # Должен быть новый dict, не тот же объект.
        assert result is not base

    def test_does_not_mutate_inputs(self):
        base = {"a": {"b": 1}}
        overlay = {"a": {"c": 2}}
        snapshot_base = {"a": {"b": 1}}
        snapshot_overlay = {"a": {"c": 2}}
        _deep_merge(base, overlay)
        assert base == snapshot_base
        assert overlay == snapshot_overlay

    def test_dict_overlaying_scalar_replaces(self):
        # Если base["x"] — скаляр, а overlay["x"] — dict, overlay побеждает.
        base = {"x": "flat"}
        overlay = {"x": {"nested": True}}
        assert _deep_merge(base, overlay) == {"x": {"nested": True}}

    def test_scalar_overlaying_dict_replaces(self):
        base = {"x": {"nested": True}}
        overlay = {"x": "flat"}
        assert _deep_merge(base, overlay) == {"x": "flat"}

    def test_none_overlay_replaces(self):
        # Явное null в overlay должно обнулить поле (overlay всегда побеждает).
        base = {"model": "sonnet"}
        overlay = {"model": None}
        assert _deep_merge(base, overlay) == {"model": None}


# ── _load_config с overlay ──


@pytest.fixture
def agent_yaml_factory(tmp_path):
    """Фабрика: пишет agent.yaml (+ опционально agent.local.yaml) и возвращает путь."""

    def make(base_config: dict, local_config: dict | str | None = None) -> Path:
        agent_dir = tmp_path / "agents" / base_config["name"]
        agent_dir.mkdir(parents=True)
        yaml_path = agent_dir / "agent.yaml"
        yaml_path.write_text(yaml.dump(base_config, allow_unicode=True), encoding="utf-8")

        if local_config is not None:
            local_path = agent_dir / "agent.local.yaml"
            if isinstance(local_config, str):
                # Сырой текст — для теста битого YAML.
                local_path.write_text(local_config, encoding="utf-8")
            else:
                local_path.write_text(
                    yaml.dump(local_config, allow_unicode=True), encoding="utf-8"
                )
        return yaml_path

    return make


def _minimal_base():
    return {
        "name": "overlay_test",
        "bot_token": "123:ABC",
        "claude_model": "sonnet",
        "allowed_users": [111, 222],
        "system_prompt": "default prompt",
        "sandbox": {"enabled": True, "bubblewrap": True},
    }


class TestLoadConfigOverlay:
    def test_no_local_returns_base(self, agent_yaml_factory):
        path = agent_yaml_factory(_minimal_base())
        agent = Agent(str(path))
        assert agent.claude_model == "sonnet"
        assert agent.allowed_users == [111, 222]
        assert agent.config["sandbox"] == {"enabled": True, "bubblewrap": True}

    def test_local_overrides_scalar_and_list(self, agent_yaml_factory):
        path = agent_yaml_factory(
            _minimal_base(),
            {"claude_model": "opus", "allowed_users": [44117786]},
        )
        agent = Agent(str(path))
        # Скаляр и список переопределены, остальное — из base.
        assert agent.claude_model == "opus"
        assert agent.allowed_users == [44117786]
        assert agent.config["sandbox"] == {"enabled": True, "bubblewrap": True}
        assert agent.system_prompt_template == "default prompt"

    def test_local_nested_dict_merge(self, agent_yaml_factory):
        path = agent_yaml_factory(
            _minimal_base(),
            {"sandbox": {"bubblewrap": False}},  # только переопределить флаг
        )
        agent = Agent(str(path))
        # enabled остался из base, bubblewrap перезаписан.
        assert agent.config["sandbox"] == {"enabled": True, "bubblewrap": False}

    def test_malformed_local_falls_back_to_base(
        self, agent_yaml_factory, caplog
    ):
        path = agent_yaml_factory(
            _minimal_base(),
            "claude_model: [unclosed\n",  # битый YAML
        )
        with caplog.at_level(logging.ERROR):
            agent = Agent(str(path))
        # Base значения сохранены, overlay проигнорирован.
        assert agent.claude_model == "sonnet"
        assert agent.allowed_users == [111, 222]
        assert any("agent.local.yaml" in r.message for r in caplog.records)

    def test_non_mapping_local_falls_back_to_base(
        self, agent_yaml_factory, caplog
    ):
        # agent.local.yaml — валидный YAML, но список, а не dict → игнор.
        path = agent_yaml_factory(_minimal_base(), "- just\n- a\n- list\n")
        with caplog.at_level(logging.ERROR):
            agent = Agent(str(path))
        assert agent.claude_model == "sonnet"
        assert any("mapping" in r.message for r in caplog.records)

    def test_empty_local_is_noop(self, agent_yaml_factory):
        # Пустой файл → safe_load возвращает None → трактуем как пустой overlay.
        path = agent_yaml_factory(_minimal_base(), "")
        agent = Agent(str(path))
        assert agent.claude_model == "sonnet"
        assert agent.allowed_users == [111, 222]

    def test_local_expands_env_vars(self, agent_yaml_factory, monkeypatch):
        monkeypatch.setenv("OVERLAY_TEST_TOKEN", "secret-from-env")
        path = agent_yaml_factory(
            _minimal_base(),
            {"bot_token": "${OVERLAY_TEST_TOKEN}"},
        )
        agent = Agent(str(path))
        assert agent.bot_token == "secret-from-env"
