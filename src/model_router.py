"""
Model router — дешёвый Haiku-классификатор перед основным вызовом.

Много сообщений мастер-агенту — простые: приветствия, короткие вопросы
из памяти, благодарности, статусы. Им не нужна мощность Sonnet/Opus.
Haiku 4.5 классифицирует каждый запрос и, если он SIMPLE, переключает
основной вызов на дешёвую модель. При COMPLEX — остаётся на тяжёлой
модели (поведение по умолчанию).

Опт-ин на уровне agent.yaml:
  claude_model_router: "haiku"   # модель для классификации
  claude_model_simple: "haiku"   # модель для SIMPLE-запросов
  claude_model: "sonnet"         # модель для COMPLEX (как раньше)

Если classifier падает/тормозит — возвращаем COMPLEX (fall back на старое
поведение). Классификатор не должен ломать основной путь.
"""

import asyncio
import logging
from typing import Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import get_claude_cli_path

logger = logging.getLogger(__name__)

Classification = Literal["SIMPLE", "COMPLEX"]

# Системный промпт классификатора — стабилен, попадает в кэш Claude CLI.
CLASSIFIER_SYSTEM = """Ты — быстрый классификатор сообщений пользователя персональному ассистенту.

Отнеси каждое сообщение ровно к одной категории и верни ОДНО слово без пояснений:

SIMPLE — приветствие, благодарность, короткий вопрос из памяти
("что было вчера", "напомни про X"), уточнение уже обсуждавшегося,
подтверждение ("ок", "да", "понял"), запрос статуса, мелкая правка текста.

COMPLEX — анализ документа/кода, исследование, планирование,
стратегическое решение, многошаговая задача, делегирование воркеру,
написание длинного текста, дебаг, всё что требует рассуждения.

При сомнении → COMPLEX. Лучше потратить лишние токены, чем недодумать.

Отвечай ТОЛЬКО одним словом: SIMPLE или COMPLEX."""

# Таймаут классификатора. Haiku обычно отвечает за 200-500 мс; если что-то
# пошло не так — не ждём дольше, идём в COMPLEX.
CLASSIFIER_TIMEOUT_SEC = 3.0


async def classify(
    user_message: str,
    router_model: str = "haiku",
) -> Classification:
    """Классифицировать сообщение пользователя: SIMPLE или COMPLEX.

    При любой ошибке или таймауте — возвращает COMPLEX (безопасный fallback).
    """
    if not user_message or not user_message.strip():
        return "COMPLEX"

    options = ClaudeAgentOptions(
        model=router_model,
        permission_mode="bypassPermissions",
        cli_path=get_claude_cli_path(),
        system_prompt=CLASSIFIER_SYSTEM,
        max_turns=1,
    )

    try:
        result = await asyncio.wait_for(
            _run_classifier(user_message, options),
            timeout=CLASSIFIER_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.info("model_router: classifier timeout, fallback COMPLEX")
        return "COMPLEX"
    except Exception as e:
        logger.warning(f"model_router: classifier error {type(e).__name__}: {e}")
        return "COMPLEX"

    verdict = result.strip().upper()
    if "SIMPLE" in verdict and "COMPLEX" not in verdict:
        return "SIMPLE"
    return "COMPLEX"


async def _run_classifier(prompt: str, options: ClaudeAgentOptions) -> str:
    text = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text += block.text
        elif isinstance(msg, ResultMessage):
            if msg.result and not text:
                text = msg.result
    return text
