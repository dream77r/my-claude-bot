"""
Agent policy tests — инварианты для worker-агентов.

Worker-агент (не master) должен:
- иметь sandbox.enabled: true (hook блокирует file-tools за пределами agents/{name}/);
- иметь Bash в allowed_tools (иначе задачи с PDF/изображениями/архивами уходят в timeout);
- иметь Edit в allowed_tools (incremental wiki-updates).

Master (`me`) и trusted dev-агенты (coder) — legitimate exceptions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.agent_manager import AGENT_YAML_TEMPLATE

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

# Агенты с legitimate исключениями — каждый со ссылкой на причину.
UNSANDBOXED_AGENTS = {
    "me": "master — full access by design",
    "coder": "trusted dev-agent, работает с внешними projects/; защита через command_guard",
}

REQUIRED_WORKER_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "Bash"}


def _parse_allowed_tools(config: dict) -> set[str]:
    flags = config.get("claude_flags", [])
    for i, flag in enumerate(flags):
        if flag == "--allowedTools" and i + 1 < len(flags):
            return set(flags[i + 1].split(","))
    return set()


def _agent_configs():
    """Yield (name, config) для каждого agent.yaml в репе."""
    for yaml_path in sorted(AGENTS_DIR.glob("*/agent.yaml")):
        name = yaml_path.parent.name
        with yaml_path.open(encoding="utf-8") as f:
            yield name, yaml.safe_load(f)


def test_worker_agents_have_sandbox_enabled():
    """Каждый не-master, не-исключение worker имеет sandbox.enabled: true."""
    violations = []
    for name, config in _agent_configs():
        if name in UNSANDBOXED_AGENTS:
            continue
        sandbox = config.get("sandbox", {})
        if not sandbox.get("enabled", False):
            violations.append(name)
    assert not violations, (
        f"Worker-агенты без sandbox.enabled: {violations}. "
        f"Либо включи sandbox, либо добавь в UNSANDBOXED_AGENTS с обоснованием."
    )


def test_worker_agents_have_bash_and_edit():
    """Worker без Bash/Edit зависает на PDF/binary задачах → timeout."""
    violations = {}
    for name, config in _agent_configs():
        if name in UNSANDBOXED_AGENTS:
            # master/coder имеют собственные tool sets — не наш инвариант
            continue
        tools = _parse_allowed_tools(config)
        missing = REQUIRED_WORKER_TOOLS - tools
        if missing:
            violations[name] = sorted(missing)
    assert not violations, (
        f"Worker-агенты без обязательных tools: {violations}. "
        f"Bash+Edit нужны чтобы обрабатывать PDF/изображения/архивы внутри "
        f"своей memory/. Sandbox уже ограничивает scope."
    )


def test_new_agent_template_policy():
    """AGENT_YAML_TEMPLATE — source of truth для новых агентов."""
    # Подставим fake placeholder'ы и распарсим как yaml
    rendered = AGENT_YAML_TEMPLATE.format(
        name="testagent",
        display_name="Test",
        env_var="TEST_BOT_TOKEN",
        description="тестовый агент",
        allowed_users_yaml="  - 123\n",
        model="sonnet",
    )
    config = yaml.safe_load(rendered)

    tools = _parse_allowed_tools(config)
    missing = REQUIRED_WORKER_TOOLS - tools
    assert not missing, (
        f"AGENT_YAML_TEMPLATE создаёт агентов без {missing}. "
        f"Новые custom-агенты будут зависать на binary задачах."
    )

    sandbox = config.get("sandbox", {})
    assert sandbox.get("enabled") is True, (
        "AGENT_YAML_TEMPLATE должен создавать агентов с sandbox.enabled: true "
        "— иначе Bash в template = доступ ко всей системе."
    )
