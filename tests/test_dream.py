"""Тесты для dream.py — Dream Memory."""

import json
from pathlib import Path

import pytest

from src.dream import (
    _extract_json,
    _get_cursor,
    _save_cursor,
    _split_template,
    _substitute,
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


class TestSplitTemplate:
    def test_no_marker_returns_none_system(self):
        system, user = _split_template("just a prompt {x}")
        assert system is None
        assert user == "just a prompt {x}"

    def test_splits_on_marker(self):
        text = "INSTRUCTIONS\n\n<!-- SYSTEM/USER SPLIT -->\n\nDATA"
        system, user = _split_template(text)
        assert system == "INSTRUCTIONS"
        assert user == "DATA"

    def test_strips_whitespace(self):
        text = "  sys  \n<!-- SYSTEM/USER SPLIT -->\n  usr  "
        system, user = _split_template(text)
        assert system == "sys"
        assert user == "usr"


class TestSubstitute:
    def test_replaces_single_key(self):
        assert _substitute("hello {name}", name="world") == "hello world"

    def test_leaves_unlisted_braces_alone(self):
        """{slug} в теле шаблона не должен трогаться, если ключа нет в kwargs."""
        template = "Update wiki/{slug}.md with {facts}"
        result = _substitute(template, facts="X")
        assert result == "Update wiki/{slug}.md with X"

    def test_survives_literal_json_braces(self):
        """.format() падает на JSON-примерах в шаблоне, .replace() — нет."""
        template = 'Respond: {"facts": [...]} -- real value: {x}'
        result = _substitute(template, x="here")
        assert result == 'Respond: {"facts": [...]} -- real value: here'

    def test_multiple_keys(self):
        result = _substitute("{a}-{b}-{a}", a="X", b="Y")
        assert result == "X-Y-X"
