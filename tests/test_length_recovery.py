"""Тесты Length Recovery: continuation при stop_reason=max_tokens."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from src.agent import Agent


@pytest.fixture
def agent(tmp_path):
    agent_dir = tmp_path / "agents" / "test"
    agent_dir.mkdir(parents=True)
    (agent_dir / "memory").mkdir()
    config = {
        "name": "test",
        "display_name": "Test",
        "bot_token": "123:ABC",
        "system_prompt": "Test.",
        "memory_path": "./agents/test/memory/",
        "skills": [],
        "allowed_users": [123],
        "max_context_messages": 5,
        "claude_model": "sonnet",
    }
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return Agent(str(yaml_path))


def _mk_assistant_msg(text: str, session_id: str = "s1"):
    """Построить фейковый AssistantMessage с одним TextBlock."""
    from claude_agent_sdk import AssistantMessage, TextBlock
    return AssistantMessage(content=[TextBlock(text=text)], model="haiku", session_id=session_id)


def _mk_result_msg(stop_reason: str | None, session_id: str = "s1"):
    from claude_agent_sdk import ResultMessage
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        stop_reason=stop_reason,
        total_cost_usd=None,
        usage=None,
        result=None,
    )


class StubQuery:
    """Возвращает разные async-generator'ы на последовательных вызовах."""

    def __init__(self, streams):
        self._streams = list(streams)
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        messages = self._streams[self.call_count] if self.call_count < len(self._streams) else []
        self.call_count += 1

        async def gen():
            for m in messages:
                yield m

        return gen()


@pytest.mark.asyncio
async def test_continuation_on_max_tokens(agent):
    """max_tokens → вызов 2 с prompt='Продолжи.', текст склеивается."""
    stub = StubQuery([
        [_mk_assistant_msg("part 1 "), _mk_result_msg("max_tokens")],
        [_mk_assistant_msg("part 2"), _mk_result_msg("end_turn")],
    ])

    with patch("src.agent.query", stub), \
         patch("src.agent.memory.log_message"), \
         patch("src.agent.memory.get_recent_messages", return_value=[]), \
         patch("src.agent.git_committer.commit", new_callable=AsyncMock):
        result = await agent.call_claude("test", semaphore=asyncio.Semaphore(1))

    assert stub.call_count == 2, "Continuation should have fired exactly once"
    assert "part 1" in result and "part 2" in result


@pytest.mark.asyncio
async def test_no_continuation_on_end_turn(agent):
    """stop_reason=end_turn → никаких continuations."""
    stub = StubQuery([[
        _mk_assistant_msg("complete answer"),
        _mk_result_msg("end_turn"),
    ]])

    with patch("src.agent.query", stub), \
         patch("src.agent.memory.log_message"), \
         patch("src.agent.memory.get_recent_messages", return_value=[]), \
         patch("src.agent.git_committer.commit", new_callable=AsyncMock):
        result = await agent.call_claude("test", semaphore=asyncio.Semaphore(1))

    assert stub.call_count == 1
    assert "complete answer" in result


@pytest.mark.asyncio
async def test_continuation_capped(agent):
    """Много подряд max_tokens → после MAX_LENGTH_RECOVERY останавливаемся."""
    # 1 оригинал + 5 упорных max_tokens. Cap в коде = 2, значит 1+2=3 вызова.
    stub = StubQuery([
        [_mk_assistant_msg(f"chunk{i} "), _mk_result_msg("max_tokens")]
        for i in range(6)
    ])

    with patch("src.agent.query", stub), \
         patch("src.agent.memory.log_message"), \
         patch("src.agent.memory.get_recent_messages", return_value=[]), \
         patch("src.agent.git_committer.commit", new_callable=AsyncMock):
        result = await agent.call_claude("test", semaphore=asyncio.Semaphore(1))

    # 1 первоначальный + 2 continuation = 3 вызова
    assert stub.call_count == 3
    for i in range(3):
        assert f"chunk{i}" in result
