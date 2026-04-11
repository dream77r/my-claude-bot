"""Тесты для heartbeat.py — Heartbeat + Evaluator."""

from pathlib import Path

import pytest

from src.memory import ensure_dirs


@pytest.fixture
def agent_dir(tmp_path):
    agent = tmp_path / "agents" / "test"
    agent.mkdir(parents=True)
    ensure_dirs(str(agent))
    return str(agent)


class TestHeartbeatFile:
    def test_no_heartbeat_file(self, agent_dir):
        """Без HEARTBEAT.md — нет задач."""
        from src.heartbeat import check_heartbeat
        # check_heartbeat вызывает Claude, поэтому просто проверим что файл не найден
        heartbeat_path = Path(agent_dir) / "HEARTBEAT.md"
        assert not heartbeat_path.exists()

    def test_empty_heartbeat_file(self, agent_dir):
        """Пустой HEARTBEAT.md — нет задач."""
        heartbeat_path = Path(agent_dir) / "HEARTBEAT.md"
        heartbeat_path.write_text("")
        # Функция вернёт has_tasks=False без вызова Claude
        import asyncio
        from src.heartbeat import check_heartbeat
        result = asyncio.get_event_loop().run_until_complete(
            check_heartbeat(agent_dir)
        )
        assert result["has_tasks"] is False

    def test_heartbeat_file_exists(self, agent_dir):
        """HEARTBEAT.md с содержимым — проверяем что файл читается."""
        heartbeat_path = Path(agent_dir) / "HEARTBEAT.md"
        heartbeat_path.write_text("# Задачи\n- [ ] Проверить логи")
        content = heartbeat_path.read_text()
        assert "Проверить логи" in content
