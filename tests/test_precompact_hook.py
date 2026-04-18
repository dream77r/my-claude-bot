"""Тесты для PreCompact hook — git-снапшот memory перед компакцией."""

import subprocess
from pathlib import Path

import pytest

from src.precompact_hook import append_daily_marker, snapshot_memory


@pytest.fixture
def git_memory(tmp_path):
    """Tmp memory-директория с инициализированным git repo."""
    memory = tmp_path / "memory"
    memory.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=memory, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=memory, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=memory, check=True)
    # Первый коммит (иначе HEAD не существует)
    (memory / "seed.md").write_text("seed")
    subprocess.run(["git", "add", "-A"], cwd=memory, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=memory, check=True)
    return memory


class TestSnapshotMemory:
    def test_returns_none_when_not_git(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "x.md").write_text("x")
        assert snapshot_memory(plain) is None

    def test_returns_none_when_clean(self, git_memory):
        assert snapshot_memory(git_memory) is None

    def test_commits_pending_changes(self, git_memory):
        (git_memory / "note.md").write_text("hello")
        sha = snapshot_memory(git_memory)
        assert sha is not None and len(sha) >= 7
        # Verify commit exists with expected message
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=git_memory,
            capture_output=True,
            text=True,
            check=True,
        )
        assert log.stdout.startswith("Pre-compact snapshot")

    def test_picks_up_edits_to_tracked_files(self, git_memory):
        (git_memory / "seed.md").write_text("seed-modified")
        sha = snapshot_memory(git_memory)
        assert sha is not None


class TestAppendDailyMarker:
    def test_creates_daily_file_if_missing(self, tmp_path):
        append_daily_marker(tmp_path, sha="abc1234")
        files = list((tmp_path / "daily").iterdir())
        assert len(files) == 1
        assert "Компакт сессии" in files[0].read_text()
        assert "abc1234" in files[0].read_text()

    def test_appends_without_sha(self, tmp_path):
        append_daily_marker(tmp_path, sha=None)
        files = list((tmp_path / "daily").iterdir())
        text = files[0].read_text()
        assert "Компакт сессии" in text
        assert "snapshot" not in text  # no sha → no snapshot mention

    def test_appends_to_existing_daily(self, tmp_path):
        daily = tmp_path / "daily"
        daily.mkdir()
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        existing = daily / f"{today}.md"
        existing.write_text("# Today\n\nexisting content\n")

        append_daily_marker(tmp_path, sha="xyz")
        text = existing.read_text()
        assert text.startswith("# Today")
        assert "existing content" in text
        assert "Компакт сессии" in text
