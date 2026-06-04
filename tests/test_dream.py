"""Тесты для dream.py — Dream Memory."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.dream import (
    _extract_json,
    _get_cursor,
    _phase1_with_retry,
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


class TestPhase1WithRetry:
    """Тесты retry-логики Phase 1 при ошибках парсинга JSON."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        """Успешный парсинг с первой попытки — без retry."""
        good_response = '{"facts": [{"title": "Test", "content": "data"}], "summary": "ok"}'
        with patch("src.dream._call_claude_simple", new=AsyncMock(return_value=good_response)):
            result, response = await _phase1_with_retry(
                prompt="test", model="haiku", cwd="/tmp", system_prompt=None
            )
        assert result == {"facts": [{"title": "Test", "content": "data"}], "summary": "ok"}
        assert response == good_response

    @pytest.mark.asyncio
    async def test_success_on_second_attempt(self):
        """Первая попытка возвращает не-JSON, вторая — валидный JSON."""
        bad = "Извините, вот мой ответ: просто текст"
        good = '{"facts": [], "summary": "retry worked"}'
        call_mock = AsyncMock(side_effect=[bad, good])
        with patch("src.dream._call_claude_simple", new=call_mock):
            result, response = await _phase1_with_retry(
                prompt="test", model="haiku", cwd="/tmp", system_prompt=None
            )
        assert result == {"facts": [], "summary": "retry worked"}
        assert call_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_strict_prompt_on_retry(self):
        """На retry к промпту добавляется строгий суффикс."""
        bad = "просто текст"
        good = '{"facts": [], "summary": "ok"}'
        call_mock = AsyncMock(side_effect=[bad, good])
        with patch("src.dream._call_claude_simple", new=call_mock):
            await _phase1_with_retry(
                prompt="base prompt", model="haiku", cwd="/tmp", system_prompt=None
            )
        # prompt передаётся первым позиционным аргументом
        first_prompt = call_mock.call_args_list[0].args[0]
        retry_prompt = call_mock.call_args_list[1].args[0]
        assert first_prompt == "base prompt"
        assert retry_prompt.startswith("base prompt")
        assert "ВАЖНО" in retry_prompt

    @pytest.mark.asyncio
    async def test_all_attempts_fail_returns_none(self):
        """Если все попытки вернули не-JSON — возвращаем None без исключения."""
        bad = "вообще не JSON"
        call_mock = AsyncMock(return_value=bad)
        with patch("src.dream._call_claude_simple", new=call_mock):
            result, last_response = await _phase1_with_retry(
                prompt="test", model="haiku", cwd="/tmp", system_prompt=None, max_retries=2
            )
        assert result is None
        assert last_response == bad
        assert call_mock.call_count == 3  # 1 initial + 2 retries

    @pytest.mark.asyncio
    async def test_llm_exception_on_first_retries_and_succeeds(self):
        """LLM бросает исключение на первой попытке, успешно на второй."""
        good = '{"facts": [], "summary": "recovered"}'
        call_mock = AsyncMock(side_effect=[RuntimeError("timeout"), good])
        with patch("src.dream._call_claude_simple", new=call_mock):
            result, _ = await _phase1_with_retry(
                prompt="test", model="haiku", cwd="/tmp", system_prompt=None
            )
        assert result == {"facts": [], "summary": "recovered"}

    @pytest.mark.asyncio
    async def test_all_llm_exceptions_returns_none(self):
        """Все попытки бросают исключение — возвращаем None, не крашимся."""
        call_mock = AsyncMock(side_effect=RuntimeError("network error"))
        with patch("src.dream._call_claude_simple", new=call_mock):
            result, last_response = await _phase1_with_retry(
                prompt="test", model="haiku", cwd="/tmp", system_prompt=None, max_retries=1
            )
        assert result is None
        assert last_response == ""  # ничего не получили


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
