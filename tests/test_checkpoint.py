"""Тесты для Checkpoint Recovery."""

import json
from pathlib import Path

import pytest

from src.checkpoint import (
    clear,
    format_recovery_message,
    make_checkpoint_hooks,
    mark_error,
    recover,
    save,
    update_text,
    update_tool,
)


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agents" / "test"
    (d / "memory" / "sessions").mkdir(parents=True)
    return str(d)


def _cp_path(agent_dir):
    return Path(agent_dir) / "memory" / "sessions" / "checkpoint.json"


class TestSave:
    def test_creates_file(self, agent_dir):
        save(agent_dir, "test prompt")
        assert _cp_path(agent_dir).exists()

    def test_stores_fields(self, agent_dir):
        save(agent_dir, "hello world", session_id="sess-123")
        data = json.loads(_cp_path(agent_dir).read_text())
        assert data["prompt"] == "hello world"
        assert data["session_id"] == "sess-123"
        assert data["status"] == "in_progress"
        assert data["tools_used"] == []

    def test_truncates_long_prompt(self, agent_dir):
        save(agent_dir, "x" * 1000)
        data = json.loads(_cp_path(agent_dir).read_text())
        assert len(data["prompt"]) == 500


class TestUpdateTool:
    def test_appends_tool(self, agent_dir):
        save(agent_dir, "test")
        update_tool(agent_dir, "Read")
        update_tool(agent_dir, "Bash")
        data = json.loads(_cp_path(agent_dir).read_text())
        assert len(data["tools_used"]) == 2
        assert data["tools_used"][0]["tool"] == "Read"
        assert data["tools_used"][1]["tool"] == "Bash"

    def test_limits_to_20(self, agent_dir):
        save(agent_dir, "test")
        for i in range(25):
            update_tool(agent_dir, f"tool_{i}")
        data = json.loads(_cp_path(agent_dir).read_text())
        assert len(data["tools_used"]) == 20

    def test_noop_without_checkpoint(self, agent_dir):
        update_tool(agent_dir, "Read")  # Не должен крэшнуть


class TestUpdateText:
    def test_updates_partial(self, agent_dir):
        save(agent_dir, "test")
        update_text(agent_dir, "partial answer...")
        data = json.loads(_cp_path(agent_dir).read_text())
        assert data["partial_text"] == "partial answer..."

    def test_truncates_long_text(self, agent_dir):
        save(agent_dir, "test")
        update_text(agent_dir, "y" * 1000)
        data = json.loads(_cp_path(agent_dir).read_text())
        assert len(data["partial_text"]) == 500


class TestMarkError:
    def test_marks_error(self, agent_dir):
        save(agent_dir, "test")
        mark_error(agent_dir, "connection timeout")
        data = json.loads(_cp_path(agent_dir).read_text())
        assert data["status"] == "error"
        assert data["error"] == "connection timeout"
        assert "ended_at" in data


class TestClear:
    def test_removes_file(self, agent_dir):
        save(agent_dir, "test")
        assert _cp_path(agent_dir).exists()
        clear(agent_dir)
        assert not _cp_path(agent_dir).exists()

    def test_noop_if_missing(self, agent_dir):
        clear(agent_dir)  # Не должен крэшнуть


class TestRecover:
    def test_returns_none_if_no_checkpoint(self, agent_dir):
        assert recover(agent_dir) is None

    def test_returns_data_for_in_progress(self, agent_dir):
        save(agent_dir, "interrupted prompt")
        update_tool(agent_dir, "Bash")
        result = recover(agent_dir)
        assert result is not None
        assert result["prompt"] == "interrupted prompt"
        assert len(result["tools_used"]) == 1

    def test_clears_error_checkpoint(self, agent_dir):
        save(agent_dir, "test")
        mark_error(agent_dir, "err")
        result = recover(agent_dir)
        assert result is None  # error = уже обработано
        assert not _cp_path(agent_dir).exists()

    def test_handles_corrupt_file(self, agent_dir):
        _cp_path(agent_dir).write_text("not json")
        result = recover(agent_dir)
        assert result is None
        assert not _cp_path(agent_dir).exists()


class TestFormatRecoveryMessage:
    def test_formats_message(self):
        data = {
            "prompt": "Напиши код для парсинга JSON",
            "started_at": "2026-04-11T10:30:00",
            "tools_used": [{"tool": "Read", "ts": "..."}, {"tool": "Bash", "ts": "..."}],
            "partial_text": "Вот пример кода...",
        }
        msg = format_recovery_message(data)
        assert "прерванный" in msg
        assert "Напиши код" in msg
        assert "Read" in msg
        assert "Bash" in msg


@pytest.mark.asyncio
class TestCheckpointHooks:
    async def test_lifecycle(self, agent_dir):
        from src.hooks import HookContext

        before_fn, tool_fn, after_fn, error_fn = make_checkpoint_hooks(agent_dir)

        # before → создаёт checkpoint
        await before_fn(HookContext(
            event="before_call", agent_name="test",
            data={"message": "test prompt"},
        ))
        assert _cp_path(agent_dir).exists()

        # tool → обновляет
        await tool_fn(HookContext(
            event="on_tool_use", agent_name="test",
            data={"tool_name": "Read"},
        ))

        # after → удаляет
        await after_fn(HookContext(
            event="after_call", agent_name="test",
            data={"response": "done"},
        ))
        assert not _cp_path(agent_dir).exists()

    async def test_error_marks(self, agent_dir):
        from src.hooks import HookContext

        before_fn, tool_fn, after_fn, error_fn = make_checkpoint_hooks(agent_dir)

        await before_fn(HookContext(
            event="before_call", agent_name="test",
            data={"message": "test"},
        ))

        await error_fn(HookContext(
            event="on_error", agent_name="test",
            data={"error": "timeout"},
        ))

        data = json.loads(_cp_path(agent_dir).read_text())
        assert data["status"] == "error"
