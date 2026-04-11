"""Тесты для dream.py — Dream Memory."""

import json
from pathlib import Path

import pytest

from src.dream import (
    _extract_json,
    _get_cursor,
    _save_cursor,
    get_unprocessed_messages,
)
from src.memory import ensure_dirs, get_memory_path, log_message


@pytest.fixture
def agent_dir(tmp_path):
    """Создать временную директорию агента."""
    agent = tmp_path / "agents" / "test"
    agent.mkdir(parents=True)
    ensure_dirs(str(agent))
    return str(agent)


class TestCursor:
    def test_no_cursor(self, agent_dir):
        assert _get_cursor(agent_dir) is None

    def test_save_and_get(self, agent_dir):
        _save_cursor(agent_dir, "2026-04-10T14:30:00")
        assert _get_cursor(agent_dir) == "2026-04-10T14:30:00"

    def test_overwrite_cursor(self, agent_dir):
        _save_cursor(agent_dir, "2026-04-10T14:00:00")
        _save_cursor(agent_dir, "2026-04-10T15:00:00")
        assert _get_cursor(agent_dir) == "2026-04-10T15:00:00"


class TestGetUnprocessedMessages:
    def test_all_messages_when_no_cursor(self, agent_dir):
        log_message(agent_dir, "user", "Сообщение 1")
        log_message(agent_dir, "user", "Сообщение 2")
        msgs = get_unprocessed_messages(agent_dir)
        assert len(msgs) == 2

    def test_filters_by_cursor(self, agent_dir):
        from datetime import datetime

        # Логируем с явной датой
        dt1 = datetime(2026, 4, 10, 14, 0)
        dt2 = datetime(2026, 4, 10, 15, 0)
        dt3 = datetime(2026, 4, 10, 16, 0)
        log_message(agent_dir, "user", "Старое", date=dt1)
        log_message(agent_dir, "user", "Среднее", date=dt2)
        log_message(agent_dir, "user", "Новое", date=dt3)

        # Курсор после среднего
        _save_cursor(agent_dir, dt2.isoformat())

        msgs = get_unprocessed_messages(agent_dir)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Новое"

    def test_empty_when_no_messages(self, agent_dir):
        msgs = get_unprocessed_messages(agent_dir)
        assert msgs == []


class TestExtractJson:
    def test_json_block(self):
        text = 'Вот результат:\n```json\n{"facts": [], "summary": "нет"}\n```\nГотово.'
        result = _extract_json(text)
        assert result == {"facts": [], "summary": "нет"}

    def test_raw_json(self):
        text = '{"facts": [{"title": "test"}], "summary": "ok"}'
        result = _extract_json(text)
        assert result["facts"][0]["title"] == "test"

    def test_json_in_text(self):
        text = 'Бла-бла {"key": "value"} бла-бла'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_no_json(self):
        text = "Просто текст без JSON"
        result = _extract_json(text)
        assert result is None

    def test_invalid_json(self):
        text = '```json\n{invalid json}\n```'
        result = _extract_json(text)
        assert result is None
