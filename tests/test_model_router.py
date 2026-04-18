"""Тесты для model_router.classify — Haiku-классификатор SIMPLE/COMPLEX."""

import asyncio
from unittest.mock import patch

import pytest

from src import model_router


@pytest.mark.asyncio
async def test_empty_message_returns_complex():
    # Пустой/пробельный промпт не тратит классификатор
    result = await model_router.classify("")
    assert result == "COMPLEX"
    result = await model_router.classify("   \n  ")
    assert result == "COMPLEX"


@pytest.mark.asyncio
async def test_simple_verdict():
    async def fake_run(prompt, options):
        return "SIMPLE"

    with patch("src.model_router._run_classifier", fake_run):
        result = await model_router.classify("привет")
        assert result == "SIMPLE"


@pytest.mark.asyncio
async def test_complex_verdict():
    async def fake_run(prompt, options):
        return "COMPLEX"

    with patch("src.model_router._run_classifier", fake_run):
        result = await model_router.classify("проанализируй этот документ")
        assert result == "COMPLEX"


@pytest.mark.asyncio
async def test_verdict_with_surrounding_text():
    """Иногда Haiku добавляет кавычки/пояснения — парсинг должен быть терпимым."""
    async def fake_run(prompt, options):
        return '  SIMPLE\n'

    with patch("src.model_router._run_classifier", fake_run):
        assert await model_router.classify("привет") == "SIMPLE"


@pytest.mark.asyncio
async def test_ambiguous_verdict_falls_back_to_complex():
    """Если в ответе и SIMPLE и COMPLEX — consider COMPLEX (безопаснее)."""
    async def fake_run(prompt, options):
        return "SIMPLE или COMPLEX, сложно сказать"

    with patch("src.model_router._run_classifier", fake_run):
        assert await model_router.classify("x") == "COMPLEX"


@pytest.mark.asyncio
async def test_classifier_error_falls_back_to_complex():
    async def raising(prompt, options):
        raise RuntimeError("SDK down")

    with patch("src.model_router._run_classifier", raising):
        assert await model_router.classify("x") == "COMPLEX"


@pytest.mark.asyncio
async def test_classifier_timeout_falls_back_to_complex():
    async def slow(prompt, options):
        await asyncio.sleep(10)
        return "SIMPLE"

    with patch("src.model_router._run_classifier", slow), \
         patch("src.model_router.CLASSIFIER_TIMEOUT_SEC", 0.05):
        assert await model_router.classify("x") == "COMPLEX"


@pytest.mark.asyncio
async def test_unknown_verdict_falls_back_to_complex():
    """Любой неразобранный ответ — COMPLEX."""
    async def fake_run(prompt, options):
        return "MAYBE"

    with patch("src.model_router._run_classifier", fake_run):
        assert await model_router.classify("x") == "COMPLEX"
