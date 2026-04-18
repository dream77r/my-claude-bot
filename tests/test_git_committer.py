"""Тесты для async debounced git committer."""

import asyncio
from unittest.mock import patch

import pytest

from src.git_committer import GitCommitter


@pytest.mark.asyncio
async def test_single_commit_runs_after_delay():
    calls: list[tuple[str, str | None]] = []

    def fake_git_commit(agent_dir, message=None):
        calls.append((agent_dir, message))
        return True

    with patch("src.git_committer.memory.git_commit", fake_git_commit):
        c = GitCommitter(delay=0.05)
        await c.commit("/a", "first")
        await asyncio.sleep(0.15)  # give it time to fire
        assert calls == [("/a", "first")]


@pytest.mark.asyncio
async def test_rapid_commits_debounce_into_one():
    calls: list[tuple[str, str | None]] = []

    def fake_git_commit(agent_dir, message=None):
        calls.append((agent_dir, message))
        return True

    with patch("src.git_committer.memory.git_commit", fake_git_commit):
        c = GitCommitter(delay=0.05)
        await c.commit("/a", "msg1")
        await c.commit("/a", "msg2")
        await c.commit("/a", "msg3")
        await asyncio.sleep(0.15)
        # Debounced → single git_commit, combined message
        assert len(calls) == 1
        assert calls[0][0] == "/a"
        assert "msg1" in calls[0][1]
        assert "+2 more" in calls[0][1]


@pytest.mark.asyncio
async def test_different_dirs_are_independent():
    calls: list[tuple[str, str | None]] = []

    def fake_git_commit(agent_dir, message=None):
        calls.append((agent_dir, message))
        return True

    with patch("src.git_committer.memory.git_commit", fake_git_commit):
        c = GitCommitter(delay=0.05)
        await c.commit("/a", "x")
        await c.commit("/b", "y")
        await asyncio.sleep(0.15)
        dirs = sorted(call[0] for call in calls)
        assert dirs == ["/a", "/b"]


@pytest.mark.asyncio
async def test_flush_forces_pending_commits():
    calls: list[tuple[str, str | None]] = []

    def fake_git_commit(agent_dir, message=None):
        calls.append((agent_dir, message))
        return True

    with patch("src.git_committer.memory.git_commit", fake_git_commit):
        c = GitCommitter(delay=60.0)  # long delay, would never fire on its own
        await c.commit("/a", "pending")
        await c.commit("/b", "pending")
        assert calls == []  # nothing yet
        await c.flush()
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_commit_failure_does_not_propagate():
    def fake_git_commit(agent_dir, message=None):
        raise RuntimeError("disk full")

    with patch("src.git_committer.memory.git_commit", fake_git_commit):
        c = GitCommitter(delay=0.05)
        await c.commit("/a", "x")
        # Allow scheduled flush to run; it must swallow the error silently
        await asyncio.sleep(0.15)
        # Should not raise; subsequent commits still work
        await c.commit("/a", "y")
