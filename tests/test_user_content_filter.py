"""Тесты source-фильтрации дневного лога перед KG L1/L2 (этап 1)."""

from src.knowledge_graph import _extract_user_content


def test_only_user_dialogue_survives_intact():
    text = (
        "# 2026-04-12 Sunday\n\n"
        "**09:15** 👤 Привет, обсудим Phase 5\n"
        "**09:20** 👤 Что у нас по MVP?\n"
    )
    result = _extract_user_content(text)
    assert "Phase 5" in result
    assert "Что у нас по MVP?" in result
    assert "# 2026-04-12 Sunday" in result


def test_only_smarttrigger_yields_no_content():
    text = (
        "# 2026-04-12 Sunday\n\n"
        "**00:00** 🤖 \n"
        "### [00:00] SmartTrigger: deadline_check\n"
        "Я помогу проверить wiki...\n"
        "Где находится ваша wiki?\n"
        "\n"
        "### [04:00] SmartTrigger: deadline_check\n"
        "Ещё один пустой вопрос про wiki.\n"
    )
    result = _extract_user_content(text)
    assert "SmartTrigger" not in result
    assert "deadline_check" not in result
    assert "помогу проверить wiki" not in result
    # Заголовок дня сохраняется, но это пустышка для KG (<50 символов после strip)
    assert "# 2026-04-12 Sunday" in result


def test_user_and_smarttrigger_mixed_keeps_only_dialogue():
    text = (
        "# 2026-04-12 Sunday\n\n"
        "**00:00** 🤖 \n"
        "### [00:00] SmartTrigger: deadline_check\n"
        "Шум-шум-шум про wiki.\n"
        "\n"
        "**07:51** 👤 Тебе нужен рестарт?\n"
        "**07:51** 🤖 Нет, рестарт не нужен — обновления подхватываются сразу.\n"
        "\n"
        "### [08:00] SmartTrigger: deadline_check\n"
        "Опять пусто.\n"
        "\n"
        "**08:01** 👤 Давай обсудим Phase 5 и MVP\n"
    )
    result = _extract_user_content(text)
    assert "Тебе нужен рестарт?" in result
    assert "рестарт не нужен" in result
    assert "Phase 5 и MVP" in result
    assert "SmartTrigger" not in result
    assert "Шум-шум-шум" not in result
    assert "Опять пусто" not in result


def test_user_with_links_dnya_section_is_stripped():
    text = (
        "# 2026-04-12 Sunday\n\n"
        "**09:15** 👤 Обсуждали ProductX\n"
        "\n"
        "## Связи дня (2026-04-12)\n"
        "\n"
        "- [[ProductX]] ↔ [[FakeEntity]] — автогенерация\n"
        "\n"
        "### Упомянутые сущности\n"
        "- [[ProductX]] (project)\n"
        "- [[FakeEntity]] (entity)\n"
    )
    result = _extract_user_content(text)
    assert "Обсуждали ProductX" in result
    assert "Связи дня" not in result
    assert "Упомянутые сущности" not in result
    assert "FakeEntity" not in result


def test_empty_daily_returns_empty():
    assert _extract_user_content("") == ""
    assert _extract_user_content("   \n\n  ") == ""


def test_background_agent_without_preceding_user_is_dropped():
    text = (
        "# 2026-04-12 Sunday\n\n"
        "**08:00** 🤖 Фоновый ответ агента без вопроса — это шум.\n"
        "\n"
        "**09:00** 👤 А вот это настоящий вопрос\n"
        "**09:01** 🤖 А вот это прямой ответ\n"
        "\n"
        "**10:00** 🤖 Снова фоновое сообщение без триггера\n"
    )
    result = _extract_user_content(text)
    assert "Фоновый ответ" not in result
    assert "Снова фоновое сообщение" not in result
    assert "настоящий вопрос" in result
    assert "прямой ответ" in result
