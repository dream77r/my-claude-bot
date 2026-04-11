"""Тесты для Audit Logging."""

import json
from pathlib import Path

import pytest

from src.audit import get_recent, log_event, make_audit_hook


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agents" / "test"
    (d / "memory" / "stats").mkdir(parents=True)
    return str(d)


class TestLogEvent:
    def test_creates_file(self, agent_dir):
        log_event(agent_dir, "tool_call", agent_name="test", tool="Read")
        path = Path(agent_dir) / "memory" / "stats" / "audit.jsonl"
        assert path.exists()

    def test_writes_json(self, agent_dir):
        log_event(agent_dir, "tool_call", agent_name="me", tool="Bash", command="ls")
        path = Path(agent_dir) / "memory" / "stats" / "audit.jsonl"
        data = json.loads(path.read_text().strip())
        assert data["type"] == "tool_call"
        assert data["agent"] == "me"
        assert data["tool"] == "Bash"
        assert data["command"] == "ls"

    def test_redacts_long_strings(self, agent_dir):
        log_event(agent_dir, "tool_call", content="x" * 500)
        path = Path(agent_dir) / "memory" / "stats" / "audit.jsonl"
        data = json.loads(path.read_text().strip())
        assert len(data["content"]) <= 303  # 300 + "..."

    def test_redacts_dict_values(self, agent_dir):
        log_event(agent_dir, "tool_call", input={"code": "y" * 500})
        path = Path(agent_dir) / "memory" / "stats" / "audit.jsonl"
        data = json.loads(path.read_text().strip())
        assert len(data["input"]["code"]) <= 203

    def test_multiple_entries(self, agent_dir):
        log_event(agent_dir, "tool_call", tool="Read")
        log_event(agent_dir, "guard_block", reason="rm -rf /")
        log_event(agent_dir, "ssrf_block", url="http://localhost")
        path = Path(agent_dir) / "memory" / "stats" / "audit.jsonl"
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 3


class TestGetRecent:
    def test_empty(self, agent_dir):
        assert get_recent(agent_dir) == []

    def test_returns_entries(self, agent_dir):
        log_event(agent_dir, "tool_call", tool="Read")
        log_event(agent_dir, "tool_call", tool="Write")
        entries = get_recent(agent_dir)
        assert len(entries) == 2

    def test_respects_limit(self, agent_dir):
        for i in range(10):
            log_event(agent_dir, "tool_call", tool=f"tool_{i}")
        entries = get_recent(agent_dir, limit=3)
        assert len(entries) == 3
        assert entries[0]["tool"] == "tool_7"  # Последние 3


@pytest.mark.asyncio
class TestAuditHook:
    async def test_logs_tool_call(self, agent_dir):
        from src.hooks import HookContext

        hook_fn = make_audit_hook(agent_dir)
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={"tool_name": "Read", "tool_input": {"file_path": "/tmp/test"}},
        )
        await hook_fn(ctx)

        entries = get_recent(agent_dir)
        assert len(entries) == 1
        assert entries[0]["type"] == "tool_call"
        assert entries[0]["tool"] == "Read"

    async def test_logs_guard_block(self, agent_dir):
        from src.hooks import HookContext

        hook_fn = make_audit_hook(agent_dir)
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "guard_blocked": True,
                "guard_reason": "rm -rf / — удаление корневой ФС",
            },
        )
        await hook_fn(ctx)

        entries = get_recent(agent_dir)
        assert len(entries) == 2  # tool_call + guard_block
        types = [e["type"] for e in entries]
        assert "tool_call" in types
        assert "guard_block" in types

    async def test_logs_ssrf_block(self, agent_dir):
        from src.hooks import HookContext

        hook_fn = make_audit_hook(agent_dir)
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={
                "tool_name": "WebFetch",
                "tool_input": {"url": "http://localhost:8080"},
                "ssrf_blocked": True,
                "ssrf_reason": "localhost",
            },
        )
        await hook_fn(ctx)

        entries = get_recent(agent_dir)
        types = [e["type"] for e in entries]
        assert "ssrf_block" in types
