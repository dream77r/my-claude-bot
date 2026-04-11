"""Тесты для Sandbox — изоляция файловой системы."""

import pytest

from src.sandbox import check_tool_sandbox, is_path_allowed, make_sandbox_hook


SANDBOX_ROOT = "/home/user/my-claude-bot/agents/coder"


class TestIsPathAllowed:
    """Проверка путей."""

    def test_relative_path_allowed(self):
        ok, _ = is_path_allowed("memory/wiki/test.md", SANDBOX_ROOT)
        assert ok

    def test_inside_sandbox(self):
        ok, _ = is_path_allowed(
            "/home/user/my-claude-bot/agents/coder/memory/wiki/test.md",
            SANDBOX_ROOT,
        )
        assert ok

    def test_outside_sandbox_blocked(self):
        ok, reason = is_path_allowed(
            "/home/user/my-claude-bot/agents/me/memory/profile.md",
            SANDBOX_ROOT,
        )
        assert not ok
        assert "за пределами" in reason

    def test_root_path_blocked(self):
        ok, _ = is_path_allowed("/etc/passwd", SANDBOX_ROOT)
        assert not ok

    def test_home_blocked(self):
        ok, _ = is_path_allowed("/home/user/.env", SANDBOX_ROOT)
        assert not ok

    def test_tmp_allowed(self):
        ok, _ = is_path_allowed("/tmp/test.txt", SANDBOX_ROOT)
        assert ok

    def test_usr_bin_allowed(self):
        ok, _ = is_path_allowed("/usr/bin/python3", SANDBOX_ROOT)
        assert ok

    def test_empty_path_allowed(self):
        ok, _ = is_path_allowed("", SANDBOX_ROOT)
        assert ok

    def test_extra_allowed_path(self):
        ok, _ = is_path_allowed(
            "/home/user/shared/data.csv",
            SANDBOX_ROOT,
            extra_allowed=["/home/user/shared/"],
        )
        assert ok

    def test_extra_allowed_doesnt_open_everything(self):
        ok, _ = is_path_allowed(
            "/home/user/.env",
            SANDBOX_ROOT,
            extra_allowed=["/home/user/shared/"],
        )
        assert not ok

    def test_parent_traversal_blocked(self):
        ok, _ = is_path_allowed(
            "/home/user/my-claude-bot/agents/coder/../../.env",
            SANDBOX_ROOT,
        )
        assert not ok

    def test_other_agent_blocked(self):
        ok, _ = is_path_allowed(
            "/home/user/my-claude-bot/agents/me/agent.yaml",
            SANDBOX_ROOT,
        )
        assert not ok


class TestCheckToolSandbox:
    """Проверка tool calls."""

    def test_read_inside(self):
        ok, _ = check_tool_sandbox(
            "Read",
            {"file_path": f"{SANDBOX_ROOT}/memory/profile.md"},
            SANDBOX_ROOT,
        )
        assert ok

    def test_read_outside(self):
        ok, _ = check_tool_sandbox(
            "Read",
            {"file_path": "/home/user/my-claude-bot/agents/me/memory/profile.md"},
            SANDBOX_ROOT,
        )
        assert not ok

    def test_write_inside(self):
        ok, _ = check_tool_sandbox(
            "Write",
            {"file_path": f"{SANDBOX_ROOT}/memory/wiki/new.md"},
            SANDBOX_ROOT,
        )
        assert ok

    def test_write_outside(self):
        ok, _ = check_tool_sandbox(
            "Write",
            {"file_path": "/home/user/.env"},
            SANDBOX_ROOT,
        )
        assert not ok

    def test_edit_outside(self):
        ok, _ = check_tool_sandbox(
            "Edit",
            {"file_path": "/home/user/my-claude-bot/agents/me/agent.yaml"},
            SANDBOX_ROOT,
        )
        assert not ok

    def test_glob_inside(self):
        ok, _ = check_tool_sandbox(
            "Glob",
            {"path": f"{SANDBOX_ROOT}/memory/"},
            SANDBOX_ROOT,
        )
        assert ok

    def test_glob_outside(self):
        ok, _ = check_tool_sandbox(
            "Glob",
            {"path": "/home/user/"},
            SANDBOX_ROOT,
        )
        assert not ok

    def test_grep_no_path(self):
        ok, _ = check_tool_sandbox(
            "Grep",
            {"pattern": "test"},
            SANDBOX_ROOT,
        )
        assert ok

    def test_bash_safe_command(self):
        ok, _ = check_tool_sandbox(
            "Bash",
            {"command": "ls -la"},
            SANDBOX_ROOT,
        )
        assert ok

    def test_bash_abs_path_outside(self):
        ok, _ = check_tool_sandbox(
            "Bash",
            {"command": "cat /home/user/my-claude-bot/agents/me/memory/profile.md"},
            SANDBOX_ROOT,
        )
        assert not ok

    def test_bash_abs_path_inside(self):
        ok, _ = check_tool_sandbox(
            "Bash",
            {"command": f"cat {SANDBOX_ROOT}/memory/profile.md"},
            SANDBOX_ROOT,
        )
        assert ok

    def test_bash_tmp_allowed(self):
        ok, _ = check_tool_sandbox(
            "Bash",
            {"command": "cat /tmp/test.txt"},
            SANDBOX_ROOT,
        )
        assert ok

    def test_websearch_allowed(self):
        ok, _ = check_tool_sandbox(
            "WebSearch",
            {"query": "python asyncio"},
            SANDBOX_ROOT,
        )
        assert ok

    def test_bash_relative_command(self):
        ok, _ = check_tool_sandbox(
            "Bash",
            {"command": "cat memory/profile.md"},
            SANDBOX_ROOT,
        )
        assert ok


@pytest.mark.asyncio
class TestSandboxHook:
    """Тесты on_tool_use хука."""

    async def test_allows_inside(self):
        from src.hooks import HookContext

        hook_fn = make_sandbox_hook(SANDBOX_ROOT)
        ctx = HookContext(
            event="on_tool_use",
            agent_name="coder",
            data={
                "tool_name": "Read",
                "tool_input": {"file_path": f"{SANDBOX_ROOT}/memory/test.md"},
            },
        )
        result = await hook_fn(ctx)
        assert "sandbox_blocked" not in result.data

    async def test_blocks_outside(self):
        from src.hooks import HookContext

        hook_fn = make_sandbox_hook(SANDBOX_ROOT)
        ctx = HookContext(
            event="on_tool_use",
            agent_name="coder",
            data={
                "tool_name": "Read",
                "tool_input": {"file_path": "/home/user/.env"},
            },
        )
        result = await hook_fn(ctx)
        assert result.data.get("sandbox_blocked") is True

    async def test_extra_paths(self):
        from src.hooks import HookContext

        hook_fn = make_sandbox_hook(SANDBOX_ROOT, extra_allowed=["/home/user/shared/"])
        ctx = HookContext(
            event="on_tool_use",
            agent_name="coder",
            data={
                "tool_name": "Read",
                "tool_input": {"file_path": "/home/user/shared/data.csv"},
            },
        )
        result = await hook_fn(ctx)
        assert "sandbox_blocked" not in result.data
