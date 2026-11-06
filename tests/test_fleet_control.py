"""Политика флота: выключение, глушение уведомлений, доступ."""

from types import SimpleNamespace

import pytest

from src.bridge.fleet import FleetCommandsMixin
from src.fleet_control import (
    load_flags,
    read_allowed_users,
    should_drop_proactive,
    update_flags,
    write_allowed_users,
)
from src.main import load_agents


def _agent(root, name, role="worker"):
    agent_dir = root / "agents" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        f"name: {name}\nrole: {role}\nbot_token: '1:test'\n"
        "allowed_users:\n  - 100\n",
        encoding="utf-8",
    )
    return agent_dir


def test_defaults_are_on(tmp_path):
    agent_dir = _agent(tmp_path, "coder")
    flags = load_flags(agent_dir)
    assert flags.enabled is True
    assert flags.notifications is True


def test_update_preserves_other_local_text(tmp_path):
    agent_dir = _agent(tmp_path, "coder")
    local = agent_dir / "agent.local.yaml"
    local.write_text("allowed_users:\n  - 7\n# не трогать\n", encoding="utf-8")

    flags = update_flags(agent_dir, enabled=False)

    text = local.read_text(encoding="utf-8")
    assert "allowed_users:" in text
    assert "# не трогать" in text
    assert flags.enabled is False
    assert flags.notifications is True
    assert load_flags(agent_dir).enabled is False


def test_second_update_replaces_block(tmp_path):
    agent_dir = _agent(tmp_path, "coder")
    update_flags(agent_dir, enabled=False)
    update_flags(agent_dir, notifications=False)
    text = (agent_dir / "agent.local.yaml").read_text(encoding="utf-8")
    assert text.count("# >>> fleet-control") == 1
    flags = load_flags(agent_dir)
    assert flags.enabled is False
    assert flags.notifications is False


def test_mute_drops_proactive_but_not_reply(tmp_path):
    coder = _agent(tmp_path, "coder")
    me = _agent(tmp_path, "me", role="master")
    update_flags(coder, notifications=False)

    proactive = SimpleNamespace(
        source="agent:coder",
        metadata={"proactive": True, "origin_agent": "coder"},
    )
    reply = SimpleNamespace(
        source="agent:coder",
        metadata={"event": "text_delta"},
    )
    assert should_drop_proactive(proactive, me) is True
    assert should_drop_proactive(reply, me) is False


def test_disabled_agent_is_not_loaded(tmp_path):
    _agent(tmp_path, "coder")
    update_flags(tmp_path / "agents" / "coder", enabled=False)
    assert load_agents(tmp_path) == []


def test_allowed_users_overlay_replaces_base_and_keeps_comments(tmp_path):
    agent_dir = _agent(tmp_path, "team")
    local = agent_dir / "agent.local.yaml"
    local.write_text("# локальное\nnote: keep\n", encoding="utf-8")

    write_allowed_users(agent_dir, [5, 5, 9])

    text = local.read_text(encoding="utf-8")
    assert "# локальное" in text
    assert "note: keep" in text
    assert read_allowed_users(agent_dir) == [5, 9]
    write_allowed_users(agent_dir, [])
    assert read_allowed_users(agent_dir) == []


@pytest.mark.asyncio
async def test_worker_cannot_disable(tmp_path):
    bot = FleetCommandsMixin()
    bot.agent = SimpleNamespace(is_master=False, name="coder")
    bot.fleet_runtime = SimpleNamespace(root=tmp_path)
    sent = {}

    async def _reply(update, context, text):
        sent["text"] = text

    bot._reply = _reply
    await bot._cmd_disable_agent(None, None, "coder")
    assert "оркестратор" in sent["text"].lower()


@pytest.mark.asyncio
async def test_master_disable_stops_and_persists(tmp_path):
    _agent(tmp_path, "coder")
    stopped = []

    class Runtime:
        root = tmp_path

        def is_running(self, name):
            return name == "coder"

        async def stop_agent(self, name):
            stopped.append(name)
            return True, "остановлен"

    bot = FleetCommandsMixin()
    bot.agent = SimpleNamespace(is_master=True, name="me")
    bot.fleet_runtime = Runtime()
    sent = {}

    async def _reply(update, context, text):
        sent["text"] = text

    bot._reply = _reply
    await bot._cmd_disable_agent(None, None, "coder")

    assert stopped == ["coder"]
    assert load_flags(tmp_path / "agents" / "coder").enabled is False
    assert "выключен" in sent["text"].lower()


@pytest.mark.asyncio
async def test_master_cannot_disable_itself(tmp_path):
    _agent(tmp_path, "me", role="master")
    bot = FleetCommandsMixin()
    bot.agent = SimpleNamespace(is_master=True, name="me")
    bot.fleet_runtime = SimpleNamespace(root=tmp_path, is_running=lambda name: False)
    sent = {}

    async def _reply(update, context, text):
        sent["text"] = text

    bot._reply = _reply
    await bot._cmd_disable_agent(None, None, "me")
    assert "нельзя" in sent["text"].lower()
    assert not (tmp_path / "agents" / "me" / "agent.local.yaml").exists()
