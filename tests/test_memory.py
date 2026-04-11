"""Тесты для memory.py."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.memory import (
    archive_old_conversations,
    clear_session_id,
    create_group_context,
    ensure_daily_note,
    ensure_dirs,
    ensure_group_dirs,
    get_group_setting,
    get_memory_path,
    get_recent_messages,
    get_session_id,
    get_setting,
    is_group_onboarding_needed,
    log_group_message,
    log_message,
    read_context,
    read_group_context,
    save_session_id,
    set_group_setting,
    update_group_rules,
)


@pytest.fixture
def agent_dir(tmp_path):
    """Создать временную директорию агента."""
    agent = tmp_path / "agents" / "test"
    agent.mkdir(parents=True)
    ensure_dirs(str(agent))
    return str(agent)


class TestEnsureDirs:
    def test_creates_all_dirs(self, agent_dir):
        memory = get_memory_path(agent_dir)
        assert (memory / "daily").is_dir()
        assert (memory / "wiki" / "entities").is_dir()
        assert (memory / "wiki" / "concepts").is_dir()
        assert (memory / "wiki" / "synthesis").is_dir()
        assert (memory / "raw" / "files").is_dir()
        assert (memory / "raw" / "conversations").is_dir()
        assert (memory / "sessions").is_dir()

    def test_idempotent(self, agent_dir):
        ensure_dirs(agent_dir)
        ensure_dirs(agent_dir)  # не должно падать


class TestEnsureDailyNote:
    def test_creates_note(self, agent_dir):
        date = datetime(2026, 4, 10)
        path = ensure_daily_note(agent_dir, date)
        assert path.exists()
        assert path.name == "2026-04-10.md"
        content = path.read_text()
        assert "2026-04-10" in content

    def test_does_not_overwrite(self, agent_dir):
        date = datetime(2026, 4, 10)
        path = ensure_daily_note(agent_dir, date)
        path.write_text("custom content")
        path2 = ensure_daily_note(agent_dir, date)
        assert path2.read_text() == "custom content"

    def test_default_date(self, agent_dir):
        path = ensure_daily_note(agent_dir)
        today = datetime.now().strftime("%Y-%m-%d")
        assert today in path.name


class TestLogMessage:
    def test_log_user_message(self, agent_dir):
        date = datetime(2026, 4, 10, 14, 30)
        log_message(agent_dir, "user", "Привет!", date=date)

        # Проверить daily note
        daily = get_memory_path(agent_dir) / "daily" / "2026-04-10.md"
        content = daily.read_text()
        assert "14:30" in content
        assert "Привет!" in content

        # Проверить conversations.jsonl
        conv = get_memory_path(agent_dir) / "raw" / "conversations" / "conversations-2026-04-10.jsonl"
        assert conv.exists()
        record = json.loads(conv.read_text().strip())
        assert record["role"] == "user"
        assert record["content"] == "Привет!"

        # Проверить log.md
        log = get_memory_path(agent_dir) / "log.md"
        assert log.exists()
        assert "user: Привет!" in log.read_text()

    def test_log_with_files(self, agent_dir):
        log_message(agent_dir, "user", "Вот файл", files=["/tmp/test.pdf"])
        daily = get_memory_path(agent_dir) / "daily" / (datetime.now().strftime("%Y-%m-%d") + ".md")
        content = daily.read_text()
        assert "test.pdf" in content

    def test_log_assistant_message(self, agent_dir):
        log_message(agent_dir, "assistant", "Вот ответ")
        log = get_memory_path(agent_dir) / "log.md"
        assert "assistant: Вот ответ" in log.read_text()

    def test_truncates_long_message_in_daily(self, agent_dir):
        long_msg = "x" * 1000
        log_message(agent_dir, "user", long_msg)
        daily_dir = get_memory_path(agent_dir) / "daily"
        files = list(daily_dir.glob("*.md"))
        content = files[0].read_text()
        # daily note обрезает до 500 символов
        assert len(content) < 700


class TestReadContext:
    def test_reads_profile_and_index(self, agent_dir):
        memory = get_memory_path(agent_dir)
        (memory / "profile.md").write_text("# Профиль\nФаундер, 35 лет")
        (memory / "index.md").write_text("# Каталог\n- Проект X")

        ctx = read_context(agent_dir)
        assert "Фаундер" in ctx
        assert "Проект X" in ctx

    def test_empty_if_no_files(self, agent_dir):
        ctx = read_context(agent_dir)
        assert ctx == ""  # Нет файлов — пустой контекст

    def test_includes_daily_note(self, agent_dir):
        log_message(agent_dir, "user", "Тестовое сообщение")
        ctx = read_context(agent_dir)
        assert "Тестовое сообщение" in ctx


class TestSessionId:
    def test_save_and_get(self, agent_dir):
        save_session_id(agent_dir, "abc-123")
        assert get_session_id(agent_dir) == "abc-123"

    def test_get_returns_none_if_missing(self, agent_dir):
        assert get_session_id(agent_dir) is None

    def test_clear(self, agent_dir):
        save_session_id(agent_dir, "abc-123")
        clear_session_id(agent_dir)
        assert get_session_id(agent_dir) is None

    def test_clear_when_no_session(self, agent_dir):
        clear_session_id(agent_dir)  # не должно падать

    def test_overwrite(self, agent_dir):
        save_session_id(agent_dir, "old-id")
        save_session_id(agent_dir, "new-id")
        assert get_session_id(agent_dir) == "new-id"


class TestArchiveOldConversations:
    def test_archives_old_files(self, agent_dir):
        memory = get_memory_path(agent_dir)
        conv_dir = memory / "raw" / "conversations"

        # Создать "старый" файл
        old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
        old_file = conv_dir / f"conversations-{old_date}.jsonl"
        old_file.write_text('{"test": true}\n')

        # Создать "новый" файл
        new_date = datetime.now().strftime("%Y-%m-%d")
        new_file = conv_dir / f"conversations-{new_date}.jsonl"
        new_file.write_text('{"test": true}\n')

        archived = archive_old_conversations(agent_dir, days=30)
        assert archived == 1
        assert not old_file.exists()
        assert new_file.exists()
        assert (conv_dir / "archive" / old_file.name).exists()

    def test_no_files_to_archive(self, agent_dir):
        archived = archive_old_conversations(agent_dir)
        assert archived == 0


class TestGetRecentMessages:
    def test_returns_messages(self, agent_dir):
        for i in range(5):
            log_message(agent_dir, "user", f"Сообщение {i}")

        msgs = get_recent_messages(agent_dir, limit=3)
        assert len(msgs) == 3
        assert msgs[-1]["content"] == "Сообщение 4"

    def test_empty_if_no_conversations(self, agent_dir):
        msgs = get_recent_messages(agent_dir)
        assert msgs == []

    def test_respects_limit(self, agent_dir):
        for i in range(10):
            log_message(agent_dir, "user", f"Msg {i}")

        msgs = get_recent_messages(agent_dir, limit=5)
        assert len(msgs) == 5


class TestGetSetting:
    def test_reads_existing_setting(self, agent_dir):
        import json
        settings_path = Path(agent_dir) / "memory" / "settings.json"
        settings_path.write_text(
            json.dumps({"deepgram_api_key": "test-key-123"}),
            encoding="utf-8",
        )
        assert get_setting(agent_dir, "deepgram_api_key") == "test-key-123"

    def test_returns_none_if_no_file(self, agent_dir):
        assert get_setting(agent_dir, "deepgram_api_key") is None

    def test_returns_none_if_key_missing(self, agent_dir):
        import json
        settings_path = Path(agent_dir) / "memory" / "settings.json"
        settings_path.write_text(
            json.dumps({"other_key": "value"}),
            encoding="utf-8",
        )
        assert get_setting(agent_dir, "deepgram_api_key") is None

    def test_handles_corrupt_json(self, agent_dir):
        settings_path = Path(agent_dir) / "memory" / "settings.json"
        settings_path.write_text("not valid json{{{", encoding="utf-8")
        assert get_setting(agent_dir, "deepgram_api_key") is None


# ── Group memory tests ──

GROUP_CHAT_ID = -1001234567890


class TestEnsureGroupDirs:
    def test_creates_group_dirs(self, agent_dir):
        ensure_group_dirs(agent_dir, GROUP_CHAT_ID)
        memory = get_memory_path(agent_dir)
        group_dir = memory / "groups" / str(GROUP_CHAT_ID)
        assert (group_dir / "daily").is_dir()
        assert (group_dir / "wiki").is_dir()

    def test_idempotent(self, agent_dir):
        ensure_group_dirs(agent_dir, GROUP_CHAT_ID)
        ensure_group_dirs(agent_dir, GROUP_CHAT_ID)  # не должно падать


class TestLogGroupMessage:
    def test_logs_message(self, agent_dir):
        date = datetime(2026, 4, 11, 9, 15)
        log_group_message(agent_dir, GROUP_CHAT_ID, "Алексей", "Релиз готов", date=date)

        memory = get_memory_path(agent_dir)
        daily = memory / "groups" / str(GROUP_CHAT_ID) / "daily" / "2026-04-11.md"
        assert daily.exists()
        content = daily.read_text()
        assert "09:15" in content
        assert "Алексей" in content
        assert "Релиз готов" in content

    def test_creates_daily_header(self, agent_dir):
        date = datetime(2026, 4, 11)
        log_group_message(agent_dir, GROUP_CHAT_ID, "Тест", "Привет", date=date)

        memory = get_memory_path(agent_dir)
        daily = memory / "groups" / str(GROUP_CHAT_ID) / "daily" / "2026-04-11.md"
        content = daily.read_text()
        assert "# 2026-04-11" in content

    def test_appends_multiple_messages(self, agent_dir):
        date = datetime(2026, 4, 11, 9, 0)
        log_group_message(agent_dir, GROUP_CHAT_ID, "Алексей", "Первое", date=date)
        date2 = datetime(2026, 4, 11, 9, 5)
        log_group_message(agent_dir, GROUP_CHAT_ID, "Марина", "Второе", date=date2)

        memory = get_memory_path(agent_dir)
        daily = memory / "groups" / str(GROUP_CHAT_ID) / "daily" / "2026-04-11.md"
        content = daily.read_text()
        assert "Алексей" in content
        assert "Марина" in content
        assert "Первое" in content
        assert "Второе" in content

    def test_truncates_long_message(self, agent_dir):
        long_msg = "x" * 1000
        log_group_message(agent_dir, GROUP_CHAT_ID, "User", long_msg)

        memory = get_memory_path(agent_dir)
        daily_dir = memory / "groups" / str(GROUP_CHAT_ID) / "daily"
        files = list(daily_dir.glob("*.md"))
        content = files[0].read_text()
        # Обрезает до 500 символов
        assert len(content) < 700

    def test_default_date(self, agent_dir):
        log_group_message(agent_dir, GROUP_CHAT_ID, "User", "Тест")
        today = datetime.now().strftime("%Y-%m-%d")
        memory = get_memory_path(agent_dir)
        daily = memory / "groups" / str(GROUP_CHAT_ID) / "daily" / f"{today}.md"
        assert daily.exists()


class TestReadGroupContext:
    def test_reads_context_md(self, agent_dir):
        ensure_group_dirs(agent_dir, GROUP_CHAT_ID)
        memory = get_memory_path(agent_dir)
        context_path = memory / "groups" / str(GROUP_CHAT_ID) / "context.md"
        context_path.write_text("# Группа: Команда\n- Тема: продукт")

        ctx = read_group_context(agent_dir, GROUP_CHAT_ID)
        assert "Команда" in ctx
        assert "продукт" in ctx

    def test_reads_daily_note(self, agent_dir):
        log_group_message(agent_dir, GROUP_CHAT_ID, "Алексей", "Тестовое сообщение")
        ctx = read_group_context(agent_dir, GROUP_CHAT_ID)
        assert "Тестовое сообщение" in ctx

    def test_empty_if_no_group(self, agent_dir):
        ctx = read_group_context(agent_dir, -999)
        assert ctx == ""

    def test_truncates_long_daily(self, agent_dir):
        # Создать длинный лог
        for i in range(200):
            log_group_message(
                agent_dir, GROUP_CHAT_ID, "User", f"Сообщение номер {i} " + "x" * 50
            )
        ctx = read_group_context(agent_dir, GROUP_CHAT_ID)
        # Должен содержать контекст, но обрезанный
        assert "обрезано" in ctx or len(ctx) < 12000


class TestIsGroupOnboardingNeeded:
    def test_needed_when_no_context(self, agent_dir):
        assert is_group_onboarding_needed(agent_dir, GROUP_CHAT_ID) is True

    def test_not_needed_when_context_exists(self, agent_dir):
        create_group_context(agent_dir, GROUP_CHAT_ID, "Тест", "supergroup")
        assert is_group_onboarding_needed(agent_dir, GROUP_CHAT_ID) is False


class TestCreateGroupContext:
    def test_creates_context_file(self, agent_dir):
        create_group_context(agent_dir, GROUP_CHAT_ID, "Команда Product", "supergroup")

        memory = get_memory_path(agent_dir)
        context_path = memory / "groups" / str(GROUP_CHAT_ID) / "context.md"
        assert context_path.exists()
        content = context_path.read_text()
        assert "Команда Product" in content
        assert str(GROUP_CHAT_ID) in content
        assert "supergroup" in content

    def test_creates_dirs(self, agent_dir):
        create_group_context(agent_dir, GROUP_CHAT_ID, "Тест", "group")
        memory = get_memory_path(agent_dir)
        assert (memory / "groups" / str(GROUP_CHAT_ID) / "daily").is_dir()
        assert (memory / "groups" / str(GROUP_CHAT_ID) / "wiki").is_dir()


class TestUpdateGroupRules:
    def test_adds_rules(self, agent_dir):
        create_group_context(agent_dir, GROUP_CHAT_ID, "Тест", "group")
        update_group_rules(agent_dir, GROUP_CHAT_ID, "Будь экспертом по маркетингу")

        memory = get_memory_path(agent_dir)
        context_path = memory / "groups" / str(GROUP_CHAT_ID) / "context.md"
        content = context_path.read_text()
        assert "Правила от владельца" in content
        assert "Будь экспертом по маркетингу" in content

    def test_replaces_old_rules(self, agent_dir):
        create_group_context(agent_dir, GROUP_CHAT_ID, "Тест", "group")
        update_group_rules(agent_dir, GROUP_CHAT_ID, "Старые правила")
        update_group_rules(agent_dir, GROUP_CHAT_ID, "Новые правила")

        memory = get_memory_path(agent_dir)
        context_path = memory / "groups" / str(GROUP_CHAT_ID) / "context.md"
        content = context_path.read_text()
        assert "Новые правила" in content
        assert "Старые правила" not in content
        # Секция Правила должна быть ровно одна
        assert content.count("## Правила от владельца") == 1

    def test_preserves_original_context(self, agent_dir):
        create_group_context(agent_dir, GROUP_CHAT_ID, "Команда Product", "supergroup")
        update_group_rules(agent_dir, GROUP_CHAT_ID, "Тон: дружелюбный")

        memory = get_memory_path(agent_dir)
        context_path = memory / "groups" / str(GROUP_CHAT_ID) / "context.md"
        content = context_path.read_text()
        assert "Команда Product" in content
        assert "Тон: дружелюбный" in content

    def test_noop_if_no_context_file(self, agent_dir):
        # Не должно падать если context.md не существует
        update_group_rules(agent_dir, GROUP_CHAT_ID, "Какие-то правила")

    def test_rules_in_group_system_prompt(self, agent_dir):
        """Правила от владельца попадают в read_group_context."""
        create_group_context(agent_dir, GROUP_CHAT_ID, "Тест", "group")
        update_group_rules(agent_dir, GROUP_CHAT_ID, "Отвечай только по-английски")

        ctx = read_group_context(agent_dir, GROUP_CHAT_ID)
        assert "Отвечай только по-английски" in ctx


class TestGroupSettings:
    def test_set_and_get(self, agent_dir):
        ensure_group_dirs(agent_dir, GROUP_CHAT_ID)
        set_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic", 42)
        assert get_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic") == 42

    def test_get_missing_key(self, agent_dir):
        ensure_group_dirs(agent_dir, GROUP_CHAT_ID)
        assert get_group_setting(agent_dir, GROUP_CHAT_ID, "nonexistent") is None

    def test_get_no_settings_file(self, agent_dir):
        assert get_group_setting(agent_dir, GROUP_CHAT_ID, "key") is None

    def test_overwrite_setting(self, agent_dir):
        ensure_group_dirs(agent_dir, GROUP_CHAT_ID)
        set_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic", 10)
        set_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic", 20)
        assert get_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic") == 20

    def test_remove_setting_with_none(self, agent_dir):
        ensure_group_dirs(agent_dir, GROUP_CHAT_ID)
        set_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic", 42)
        set_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic", None)
        assert get_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic") is None

    def test_multiple_settings(self, agent_dir):
        ensure_group_dirs(agent_dir, GROUP_CHAT_ID)
        set_group_setting(agent_dir, GROUP_CHAT_ID, "is_forum", True)
        set_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic", 5)
        assert get_group_setting(agent_dir, GROUP_CHAT_ID, "is_forum") is True
        assert get_group_setting(agent_dir, GROUP_CHAT_ID, "allowed_topic") == 5

    def test_creates_dirs_if_needed(self, agent_dir):
        # set_group_setting вызывает ensure_group_dirs
        set_group_setting(agent_dir, GROUP_CHAT_ID, "key", "value")
        assert get_group_setting(agent_dir, GROUP_CHAT_ID, "key") == "value"
