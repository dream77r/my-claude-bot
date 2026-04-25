"""Тесты для consolidator.py — фокус на token-budget трекинге."""

import pytest

from src.consolidator import Consolidator


@pytest.fixture
def cons(tmp_path):
    """Consolidator с дефолтным конфигом, изолированный agent_dir."""
    return Consolidator(str(tmp_path), config={})


class TestTokenUsageTracking:
    def test_update_token_usage_sums_all_three_fields(self, cons):
        """Полный размер контекста = input + cache_read + cache_creation."""
        cons.update_token_usage({
            "input_tokens": 10_000,
            "cache_read_input_tokens": 100_000,
            "cache_creation_input_tokens": 5_000,
        })
        assert cons._last_input_tokens == 115_000

    def test_update_handles_missing_fields(self, cons):
        """Если только часть полей — берём что есть, остальное 0."""
        cons.update_token_usage({"input_tokens": 50_000})
        assert cons._last_input_tokens == 50_000

    def test_update_handles_none_usage(self, cons):
        """usage=None не должен падать и не должен затирать накопленное."""
        cons.update_token_usage({"input_tokens": 1000})
        cons.update_token_usage(None)
        assert cons._last_input_tokens == 1000

    def test_update_handles_zero_usage(self, cons):
        """Все нули не затирают предыдущее значение (защита от пустого ResultMessage)."""
        cons.update_token_usage({"input_tokens": 1000})
        cons.update_token_usage({
            "input_tokens": 0,
            "cache_read_input_tokens": 0,
        })
        assert cons._last_input_tokens == 1000

    def test_update_handles_garbage_types(self, cons):
        """Не падать если Anthropic вернул что-то странное (None/строки)."""
        cons.update_token_usage({
            "input_tokens": None,
            "cache_read_input_tokens": "oops",
        })
        # Не упало — главное. Значение не обновилось.
        assert cons._last_input_tokens == 0


class TestNeedsConsolidationTokenBudget:
    def test_token_budget_triggers_at_threshold(self, tmp_path):
        cons = Consolidator(str(tmp_path), config={"max_input_tokens": 100_000})
        cons.update_token_usage({"input_tokens": 100_000})
        assert cons.needs_consolidation() is True

    def test_token_budget_below_threshold(self, tmp_path):
        cons = Consolidator(str(tmp_path), config={"max_input_tokens": 100_000})
        cons.update_token_usage({"input_tokens": 50_000})
        assert cons.needs_consolidation() is False

    def test_zero_max_input_tokens_disables_check(self, tmp_path):
        """max_input_tokens=0 → token-budget триггер выключен."""
        cons = Consolidator(
            str(tmp_path),
            config={"max_input_tokens": 0, "max_turns": 1000, "max_chars": 10**9},
        )
        cons.update_token_usage({"input_tokens": 10_000_000})
        assert cons.needs_consolidation() is False

    def test_token_budget_independent_of_other_triggers(self, tmp_path):
        """max_turns и max_chars не достигнуты, но токены — за лимитом."""
        cons = Consolidator(
            str(tmp_path),
            config={
                "max_turns": 1000,
                "max_chars": 10**9,
                "max_input_tokens": 50_000,
            },
        )
        # Один turn, мало символов, но большой контекст (например, в промпт
        # подгрузили длинный файл).
        cons.track("question", "answer")
        cons.update_token_usage({"input_tokens": 60_000})
        assert cons.needs_consolidation() is True

    def test_reset_clears_token_tracking(self, tmp_path):
        """После _reset() (вызывается из consolidate) токены сбрасываются."""
        cons = Consolidator(str(tmp_path), config={"max_input_tokens": 50_000})
        cons.update_token_usage({"input_tokens": 100_000})
        assert cons.needs_consolidation() is True
        cons._reset()
        assert cons._last_input_tokens == 0
        assert cons.needs_consolidation() is False


class TestNeedsConsolidationLegacyTriggers:
    """Регрессионные тесты — старые триггеры не сломались."""

    def test_max_turns_still_works(self, tmp_path):
        cons = Consolidator(
            str(tmp_path),
            config={"max_turns": 3, "max_chars": 10**9, "max_input_tokens": 0},
        )
        for _ in range(3):
            cons.track("q", "a")
        assert cons.needs_consolidation() is True

    def test_max_chars_still_works(self, tmp_path):
        cons = Consolidator(
            str(tmp_path),
            config={"max_turns": 1000, "max_chars": 100, "max_input_tokens": 0},
        )
        cons.track("x" * 60, "y" * 60)
        assert cons.needs_consolidation() is True
