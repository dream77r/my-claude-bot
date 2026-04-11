"""
Input Sanitizer — защита от prompt injection.

Фильтрует входящие сообщения перед передачей в LLM:
- Удаляет zero-width Unicode символы (скрытый текст)
- Обнаруживает паттерны prompt injection
- Помечает подозрительные сообщения для логирования

Два режима:
1. before_call хук — санитизация промпта перед вызовом Claude
2. dream_filter — дополнительная проверка контента перед wiki-ingest

OWASP AI Agent Security: Indirect Prompt Injection — #1 risk.
"""

import logging
import re

logger = logging.getLogger(__name__)


# Zero-width и невидимые Unicode символы
_ZERO_WIDTH_CHARS = re.compile(
    "["
    "\u200b"  # zero-width space
    "\u200c"  # zero-width non-joiner
    "\u200d"  # zero-width joiner
    "\u200e"  # left-to-right mark
    "\u200f"  # right-to-left mark
    "\u2060"  # word joiner
    "\u2061"  # function application
    "\u2062"  # invisible times
    "\u2063"  # invisible separator
    "\u2064"  # invisible plus
    "\ufeff"  # BOM / zero-width no-break space
    "\ufff9"  # interlinear annotation anchor
    "\ufffa"  # interlinear annotation separator
    "\ufffb"  # interlinear annotation terminator
    "\U000e0001"  # language tag
    "]+"
)

# Паттерны prompt injection (case-insensitive)
_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+"
            r"(instructions?|prompts?|rules?|context)",
            re.IGNORECASE,
        ),
        "ignore previous instructions",
    ),
    (
        re.compile(
            r"(disregard|forget|override)\s+(all\s+|your\s+)?"
            r"(previous|prior|above|your)\s+"
            r"(instructions?|prompts?|rules?|guidelines?)",
            re.IGNORECASE,
        ),
        "override instructions",
    ),
    (
        re.compile(
            r"you\s+are\s+now\s+(a|an|the|in)\b",
            re.IGNORECASE,
        ),
        "role reassignment",
    ),
    (
        re.compile(
            r"new\s+(system\s+)?prompt\s*:",
            re.IGNORECASE,
        ),
        "system prompt injection",
    ),
    (
        re.compile(
            r"<\s*/?\s*system\s*>",
            re.IGNORECASE,
        ),
        "system tag injection",
    ),
    (
        re.compile(
            r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>",
            re.IGNORECASE,
        ),
        "model tag injection",
    ),
    (
        re.compile(
            r"(act\s+as|pretend\s+(to\s+be|you\s+are)|"
            r"roleplay\s+as|switch\s+to)\s+(a\s+)?"
            r"(different|new|another)\s+(ai|assistant|bot|model|role)",
            re.IGNORECASE,
        ),
        "role hijacking",
    ),
    (
        re.compile(
            r"(print|output|reveal|show|display|leak|dump)\s+"
            r"(your|the|all)?\s*(system\s+prompt|instructions?|"
            r"hidden\s+(text|prompt)|secret|api\s*key|password|token)",
            re.IGNORECASE,
        ),
        "data exfiltration attempt",
    ),
    (
        re.compile(
            r"base64\s*:\s*[A-Za-z0-9+/=]{50,}",
        ),
        "suspicious base64 blob",
    ),
]

# Порог: сколько injection-паттернов = блокировка
_BLOCK_THRESHOLD = 2


def strip_zero_width(text: str) -> str:
    """Удалить zero-width Unicode символы из текста."""
    return _ZERO_WIDTH_CHARS.sub("", text)


def detect_injection(text: str) -> list[str]:
    """
    Обнаружить паттерны prompt injection.

    Returns:
        Список найденных паттернов (пусто = чисто).
    """
    findings = []
    for pattern, desc in _INJECTION_PATTERNS:
        if pattern.search(text):
            findings.append(desc)
    return findings


def sanitize(text: str) -> tuple[str, list[str]]:
    """
    Полная санитизация входящего текста.

    1. Удаляет zero-width символы
    2. Проверяет на prompt injection
    3. Логирует подозрительные сообщения

    Returns:
        (очищенный текст, список обнаруженных паттернов)
    """
    # Шаг 1: strip zero-width
    cleaned = strip_zero_width(text)

    # Шаг 2: detect injection
    findings = detect_injection(cleaned)

    if findings:
        logger.warning(
            f"Input Sanitizer: обнаружены подозрительные паттерны: "
            f"{', '.join(findings)} — текст: {cleaned[:100]}..."
        )

    return cleaned, findings


def is_blocked(findings: list[str]) -> bool:
    """Нужно ли блокировать сообщение (>=2 паттернов injection)."""
    return len(findings) >= _BLOCK_THRESHOLD


def sanitize_for_dream(text: str) -> tuple[str, list[str]]:
    """
    Санитизация контента для Dream-обработки (wiki ingest).

    Строже чем обычная санитизация:
    - Любой injection-паттерн = предупреждение
    - Помечает контент для ручной проверки при wiki-записи
    """
    cleaned, findings = sanitize(text)

    if findings:
        logger.warning(
            f"Dream Sanitizer: подозрительный контент для wiki-ingest: "
            f"{', '.join(findings)}"
        )

    return cleaned, findings


def make_sanitizer_hook():
    """
    Создать before_call хук для Hook-системы.

    Санитизирует промпт перед каждым вызовом Claude:
    - Удаляет zero-width символы
    - Логирует injection-паттерны
    - Блокирует сообщения с 2+ паттернами (заменяет на предупреждение)
    """
    from .hooks import HookContext

    async def _sanitizer_hook(ctx: HookContext) -> HookContext:
        message = ctx.data.get("message", "")
        if not message:
            return ctx

        cleaned, findings = sanitize(message)

        # Всегда применяем очищенный текст (без zero-width)
        ctx.data["message"] = cleaned

        if findings:
            ctx.data["sanitizer_findings"] = findings

            if is_blocked(findings):
                ctx.data["message"] = (
                    "[Сообщение содержало подозрительные паттерны "
                    "и было отфильтровано. Пожалуйста, переформулируй запрос.]"
                )
                ctx.data["sanitizer_blocked"] = True
                logger.warning(
                    f"Input Sanitizer BLOCKED: {len(findings)} паттернов "
                    f"от агента '{ctx.agent_name}'"
                )

        return ctx

    return _sanitizer_hook
