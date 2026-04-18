"""Тесты _build_bash_sandbox_settings — маппинг agent.yaml → SandboxSettings."""

from __future__ import annotations

import pytest

from src.agent import Agent


@pytest.fixture
def master_agent():
    return Agent("agents/me/agent.yaml")


@pytest.fixture
def worker_agent():
    return Agent("agents/coder/agent.yaml")


def test_default_no_sandbox(worker_agent):
    """Без явного opt-in bash sandbox не включается."""
    assert worker_agent._build_bash_sandbox_settings({}) is None


def test_worker_bubblewrap_enabled(worker_agent):
    settings = worker_agent._build_bash_sandbox_settings({"bubblewrap": True})
    assert settings is not None
    assert settings["enabled"] is True
    assert settings["autoAllowBashIfSandboxed"] is True


def test_master_bubblewrap_ignored_by_default(master_agent, caplog):
    """На master'а bwrap не навешивается случайно — нужен явный allow."""
    with caplog.at_level("WARNING"):
        settings = master_agent._build_bash_sandbox_settings(
            {"bubblewrap": True}
        )
    assert settings is None
    assert "master" in caplog.text.lower()


def test_master_bubblewrap_with_explicit_allow(master_agent):
    settings = master_agent._build_bash_sandbox_settings({
        "bubblewrap": True,
        "allow_master_bwrap": True,
    })
    assert settings is not None
    assert settings["enabled"] is True


def test_excluded_commands_passed_through(worker_agent):
    settings = worker_agent._build_bash_sandbox_settings({
        "bubblewrap": True,
        "bubblewrap_excluded_commands": ["git", "docker"],
    })
    assert settings["excludedCommands"] == ["git", "docker"]


def test_network_proxy_passed_through(worker_agent):
    settings = worker_agent._build_bash_sandbox_settings({
        "bubblewrap": True,
        "bubblewrap_network_proxy": 8118,
    })
    assert settings["network"] == {"httpProxyPort": 8118}


def test_nested_docker_flag(worker_agent):
    settings = worker_agent._build_bash_sandbox_settings({
        "bubblewrap": True,
        "bubblewrap_nested": True,
    })
    assert settings["enableWeakerNestedSandbox"] is True


def test_explicit_false_returns_none(worker_agent):
    settings = worker_agent._build_bash_sandbox_settings({
        "bubblewrap": False,
    })
    assert settings is None


def test_full_config(worker_agent):
    settings = worker_agent._build_bash_sandbox_settings({
        "bubblewrap": True,
        "bubblewrap_excluded_commands": ["git"],
        "bubblewrap_network_proxy": 3128,
        "bubblewrap_nested": True,
    })
    assert settings == {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "excludedCommands": ["git"],
        "network": {"httpProxyPort": 3128},
        "enableWeakerNestedSandbox": True,
    }


class TestCheckBubblewrapRequirements:
    """Startup check: агент просит bwrap → должен быть установлен."""

    def test_no_agents_want_bwrap_is_noop(self, monkeypatch):
        from src.main import _check_bubblewrap_requirements

        called = []
        monkeypatch.setattr("shutil.which", lambda _: called.append(_))

        class Stub:
            config = {"sandbox": {"bubblewrap": False}}
            name = "me"

        _check_bubblewrap_requirements([Stub()])
        # which даже не вызывался
        assert called == []

    def test_bwrap_installed_passes(self, monkeypatch, caplog):
        from src.main import _check_bubblewrap_requirements

        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/bwrap")

        class Stub:
            config = {"sandbox": {"bubblewrap": True}}
            name = "coder"

        with caplog.at_level("INFO"):
            _check_bubblewrap_requirements([Stub()])
        assert "coder" in caplog.text

    def test_bwrap_missing_exits(self, monkeypatch):
        from src.main import _check_bubblewrap_requirements

        monkeypatch.setattr("shutil.which", lambda _: None)

        class Stub:
            config = {"sandbox": {"bubblewrap": True}}
            name = "coder"

        with pytest.raises(SystemExit) as exc:
            _check_bubblewrap_requirements([Stub()])
        assert exc.value.code == 1
