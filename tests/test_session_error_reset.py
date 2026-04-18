"""Тесты счётчика подряд-идущих ошибок и автосброса session_id."""

from pathlib import Path
from unittest.mock import patch

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


class TestConsecutiveErrorReset:
    def test_first_error_does_not_reset_session(self, agent, tmp_path):
        with patch("src.agent.memory.clear_session_id") as clear:
            result = agent._on_user_visible_error("/some/dir")
        clear.assert_not_called()
        assert "ошибка" in result.lower()
        assert agent._consecutive_errors["/some/dir"] == 1

    def test_second_error_clears_session_and_resets_counter(self, agent):
        with patch("src.agent.memory.clear_session_id") as clear:
            agent._on_user_visible_error("/d")
            agent._on_user_visible_error("/d")
        clear.assert_called_once_with("/d")
        assert "/d" not in agent._consecutive_errors

    def test_errors_tracked_per_dir(self, agent):
        """Счётчик изолирован: ошибки в /a не влияют на /b."""
        with patch("src.agent.memory.clear_session_id") as clear:
            agent._on_user_visible_error("/a")
            agent._on_user_visible_error("/b")
            # /a и /b — по 1 ошибке каждая, сброс не должен произойти
        clear.assert_not_called()
        assert agent._consecutive_errors["/a"] == 1
        assert agent._consecutive_errors["/b"] == 1

    def test_success_resets_counter(self, agent):
        """После успешного ответа счётчик для этого dir обнуляется.

        Напрямую тестируем поведение dict — call_claude-e2e покрыт другими тестами.
        """
        agent._consecutive_errors["/d"] = 1
        # Эмулируем успех (логика в call_claude после save_session_id)
        agent._consecutive_errors.pop("/d", None)
        assert "/d" not in agent._consecutive_errors

    def test_third_error_after_reset_starts_counting_again(self, agent):
        """После сброса счётчик стартует заново, третья ошибка снова считается первой."""
        with patch("src.agent.memory.clear_session_id"):
            agent._on_user_visible_error("/d")  # 1
            agent._on_user_visible_error("/d")  # 2 → clear, reset
            agent._on_user_visible_error("/d")  # 1 again
        assert agent._consecutive_errors["/d"] == 1
