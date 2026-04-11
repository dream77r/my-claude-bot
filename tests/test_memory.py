"""Тесты для memory.py."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.memory import (
    CONTEXT_BUDGET,
    STOP_WORDS,
    _get_hot_pages,
    _read_daily_smart,
    _read_with_limit,
    _tokenize,
    archive_old_conversations,
    build_smart_context,
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
    search_wiki,
    set_group_setting,
    track_page_hit,
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


# ── Tokenize tests ──


class TestTokenize:
    def test_basic_tokenization(self):
        tokens = _tokenize("Привет мир это тест")
        assert "привет" in tokens
        assert "мир" in tokens
        assert "тест" in tokens

    def test_removes_stop_words(self):
        tokens = _tokenize("это в и на для")
        assert tokens == []

    def test_removes_short_words(self):
        tokens = _tokenize("a b c word")
        assert tokens == ["word"]

    def test_lowercase(self):
        tokens = _tokenize("Hello WORLD Мир")
        assert "hello" in tokens
        assert "world" in tokens
        assert "мир" in tokens

    def test_empty_string(self):
        assert _tokenize("") == []

    def test_numbers_included(self):
        tokens = _tokenize("версия 42 проект")
        assert "42" in tokens
        assert "версия" in tokens


# ── Wiki search tests ──


class TestSearchWiki:
    @pytest.fixture(autouse=True)
    def setup_wiki(self, agent_dir):
        """Создать тестовые wiki-страницы."""
        self.agent_dir = agent_dir
        memory = get_memory_path(agent_dir)

        # Страница про ценообразование
        pricing_dir = memory / "wiki" / "concepts"
        pricing_dir.mkdir(parents=True, exist_ok=True)
        (pricing_dir / "pricing.md").write_text(
            "# Стратегия ценообразования\n\n"
            "## Текущая модель\n"
            "- Freemium: бесплатный тариф + Pro за $29/мес\n"
            "- Enterprise: индивидуальные цены от $199/мес\n"
            "- Скидка 20% при годовой оплате\n\n"
            "## Принципы\n"
            "- Цена должна отражать ценность\n"
            "- Конверсия free->pro целевая: 5-8%\n",
            encoding="utf-8",
        )

        # Страница про архитектуру
        arch_dir = memory / "wiki" / "entities"
        arch_dir.mkdir(parents=True, exist_ok=True)
        (arch_dir / "architecture.md").write_text(
            "# Архитектура системы\n\n"
            "## Компоненты\n"
            "- Telegram Bot API\n"
            "- Claude CLI\n"
            "- File-based memory\n",
            encoding="utf-8",
        )

        # Страница про команду
        (arch_dir / "team.md").write_text(
            "# Команда проекта\n\n"
            "- Алексей — основатель, продукт\n"
            "- Марина — маркетинг, цены\n",
            encoding="utf-8",
        )

    def test_finds_relevant_page(self):
        results = search_wiki(self.agent_dir, "какая у нас стратегия цен")
        assert len(results) > 0
        # pricing.md должен быть первым
        assert "pricing" in results[0]["path"] or "ценообразовани" in results[0]["title"].lower()

    def test_no_results_for_greetings(self):
        results = search_wiki(self.agent_dir, "привет как дела")
        assert len(results) == 0

    def test_empty_query_returns_empty(self):
        results = search_wiki(self.agent_dir, "")
        assert results == []

    def test_stop_words_only_returns_empty(self):
        results = search_wiki(self.agent_dir, "и в на с по для")
        assert results == []

    def test_title_match_scores_higher(self):
        results = search_wiki(self.agent_dir, "архитектура")
        assert len(results) > 0
        assert "architecture" in results[0]["path"]

    def test_filename_match_bonus(self):
        results = search_wiki(self.agent_dir, "pricing")
        assert len(results) > 0
        assert "pricing" in results[0]["path"]

    def test_max_results_respected(self):
        results = search_wiki(self.agent_dir, "проект команда цены архитектура", max_results=2)
        assert len(results) <= 2

    def test_truncates_long_pages(self):
        """Длинные страницы обрезаются до _WIKI_MAX_PAGE_CHARS."""
        memory = get_memory_path(self.agent_dir)
        long_page = memory / "wiki" / "concepts" / "long-page.md"
        long_page.write_text(
            "# Длинная страница\n\n" + "уникальноеслово " * 500,
            encoding="utf-8",
        )
        results = search_wiki(self.agent_dir, "уникальноеслово")
        assert len(results) > 0
        assert results[0]["content"].endswith("...")

    def test_returns_correct_structure(self):
        results = search_wiki(self.agent_dir, "архитектура")
        assert len(results) > 0
        r = results[0]
        assert "path" in r
        assert "title" in r
        assert "content" in r
        assert "score" in r
        assert isinstance(r["score"], float)
        assert r["score"] > 0

    def test_no_wiki_dir_returns_empty(self, tmp_path):
        """Если wiki/ не существует — пустой результат."""
        empty_agent = tmp_path / "empty_agent"
        empty_agent.mkdir()
        results = search_wiki(str(empty_agent), "тест")
        assert results == []

    def test_multiple_query_words_increase_score(self):
        """Больше слов из запроса в документе -> выше score."""
        results = search_wiki(self.agent_dir, "цена freemium конверсия")
        assert len(results) > 0
        # pricing.md должен быть первым — содержит все слова
        assert "pricing" in results[0]["path"]

    def test_tracks_hits_for_returned_pages(self):
        """search_wiki записывает hit для найденных страниц."""
        memory = get_memory_path(self.agent_dir)
        search_wiki(self.agent_dir, "архитектура")
        hits_file = memory / "stats" / "page_hits.json"
        assert hits_file.exists()
        data = json.loads(hits_file.read_text())
        assert any("architecture.md" in k for k in data)


class TestReadContextWithQuery:
    def test_backward_compatibility(self, agent_dir):
        """Без user_query поведение не меняется."""
        memory = get_memory_path(agent_dir)
        (memory / "profile.md").write_text("# Профиль\nТест")
        ctx_old = read_context(agent_dir)
        ctx_new = read_context(agent_dir, user_query="")
        assert ctx_old == ctx_new

    def test_includes_wiki_when_query_matches(self, agent_dir):
        """С user_query подтягиваются wiki-страницы."""
        memory = get_memory_path(agent_dir)
        wiki_dir = memory / "wiki" / "concepts"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        (wiki_dir / "roadmap.md").write_text(
            "# Дорожная карта\n\nQ2: запуск MVP\nQ3: масштабирование\n",
            encoding="utf-8",
        )

        ctx = read_context(agent_dir, user_query="какая у нас дорожная карта")
        assert "Релевантные знания из wiki" in ctx
        assert "Дорожная карта" in ctx

    def test_no_wiki_section_when_no_matches(self, agent_dir):
        """Если ничего не найдено — секция wiki не добавляется."""
        ctx = read_context(agent_dir, user_query="привет как дела")
        assert "Релевантные знания из wiki" not in ctx


# ── Read With Limit tests ──


class TestReadWithLimit:
    def test_short_file(self, agent_dir):
        memory = get_memory_path(agent_dir)
        test_file = memory / "test.md"
        test_file.write_text("Short content")
        result = _read_with_limit(test_file, 1000)
        assert result == "Short content"

    def test_truncates_long_file(self, agent_dir):
        memory = get_memory_path(agent_dir)
        test_file = memory / "test.md"
        test_file.write_text("x" * 5000)
        result = _read_with_limit(test_file, 100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")

    def test_nonexistent_file(self, agent_dir):
        memory = get_memory_path(agent_dir)
        result = _read_with_limit(memory / "nonexistent.md", 1000)
        assert result == ""

    def test_exact_limit(self, agent_dir):
        memory = get_memory_path(agent_dir)
        test_file = memory / "test.md"
        test_file.write_text("x" * 100)
        result = _read_with_limit(test_file, 100)
        assert result == "x" * 100  # exact — no truncation


# ── Track Page Hit tests ──


class TestTrackPageHit:
    def test_creates_hits_file(self, agent_dir):
        track_page_hit(agent_dir, "wiki/concepts/pricing.md")
        memory = get_memory_path(agent_dir)
        hits_file = memory / "stats" / "page_hits.json"
        assert hits_file.exists()
        data = json.loads(hits_file.read_text())
        assert "wiki/concepts/pricing.md" in data
        assert data["wiki/concepts/pricing.md"]["hits"] == 1

    def test_increments_hits(self, agent_dir):
        track_page_hit(agent_dir, "wiki/concepts/pricing.md")
        track_page_hit(agent_dir, "wiki/concepts/pricing.md")
        track_page_hit(agent_dir, "wiki/concepts/pricing.md")

        memory = get_memory_path(agent_dir)
        hits_file = memory / "stats" / "page_hits.json"
        data = json.loads(hits_file.read_text())
        assert data["wiki/concepts/pricing.md"]["hits"] == 3

    def test_updates_last_date(self, agent_dir):
        track_page_hit(agent_dir, "wiki/concepts/pricing.md")
        memory = get_memory_path(agent_dir)
        hits_file = memory / "stats" / "page_hits.json"
        data = json.loads(hits_file.read_text())
        today = datetime.now().strftime("%Y-%m-%d")
        assert data["wiki/concepts/pricing.md"]["last"] == today

    def test_multiple_pages(self, agent_dir):
        track_page_hit(agent_dir, "wiki/concepts/pricing.md")
        track_page_hit(agent_dir, "wiki/entities/team.md")

        memory = get_memory_path(agent_dir)
        hits_file = memory / "stats" / "page_hits.json"
        data = json.loads(hits_file.read_text())
        assert len(data) == 2
        assert "wiki/concepts/pricing.md" in data
        assert "wiki/entities/team.md" in data

    def test_handles_corrupt_file(self, agent_dir):
        memory = get_memory_path(agent_dir)
        stats_dir = memory / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        (stats_dir / "page_hits.json").write_text("not json{{{")

        # Не должно падать, должно перезаписать
        track_page_hit(agent_dir, "wiki/test.md")
        data = json.loads((stats_dir / "page_hits.json").read_text())
        assert data["wiki/test.md"]["hits"] == 1


# ── Get Hot Pages tests ──


class TestGetHotPages:
    def test_returns_popular_pages(self, agent_dir):
        memory = get_memory_path(agent_dir)
        # Создать wiki-страницу
        wiki = memory / "wiki" / "concepts"
        (wiki / "pricing.md").write_text("# Ценообразование\nМодель подписки")

        # Создать stats
        today = datetime.now().strftime("%Y-%m-%d")
        stats_dir = memory / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        hits_data = {
            "wiki/concepts/pricing.md": {"hits": 10, "last": today}
        }
        (stats_dir / "page_hits.json").write_text(json.dumps(hits_data))

        result = _get_hot_pages(agent_dir, 3000)
        assert "Ценообразование" in result

    def test_excludes_cold_pages(self, agent_dir):
        memory = get_memory_path(agent_dir)
        wiki = memory / "wiki" / "concepts"
        (wiki / "old.md").write_text("# Старая страница")

        old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
        stats_dir = memory / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        hits_data = {
            "wiki/concepts/old.md": {"hits": 100, "last": old_date}
        }
        (stats_dir / "page_hits.json").write_text(json.dumps(hits_data))

        result = _get_hot_pages(agent_dir, 3000)
        assert result == ""

    def test_respects_budget(self, agent_dir):
        memory = get_memory_path(agent_dir)
        wiki = memory / "wiki" / "concepts"
        # Создать большую страницу
        (wiki / "big.md").write_text("x" * 5000)

        today = datetime.now().strftime("%Y-%m-%d")
        stats_dir = memory / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        hits_data = {"wiki/concepts/big.md": {"hits": 10, "last": today}}
        (stats_dir / "page_hits.json").write_text(json.dumps(hits_data))

        result = _get_hot_pages(agent_dir, 200)
        assert len(result) <= 203  # 200 + "..."

    def test_excludes_specified_paths(self, agent_dir):
        memory = get_memory_path(agent_dir)
        wiki = memory / "wiki" / "concepts"
        (wiki / "pricing.md").write_text("# Ценообразование")
        (wiki / "team.md").write_text("# Команда")

        today = datetime.now().strftime("%Y-%m-%d")
        stats_dir = memory / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        hits_data = {
            "wiki/concepts/pricing.md": {"hits": 10, "last": today},
            "wiki/concepts/team.md": {"hits": 5, "last": today},
        }
        (stats_dir / "page_hits.json").write_text(json.dumps(hits_data))

        result = _get_hot_pages(
            agent_dir, 3000, exclude=["wiki/concepts/pricing.md"]
        )
        assert "Ценообразование" not in result
        assert "Команда" in result

    def test_no_stats_file(self, agent_dir):
        result = _get_hot_pages(agent_dir, 3000)
        assert result == ""

    def test_decay_scoring(self, agent_dir):
        """Страница с меньшим кол-вом хитов, но более свежая,
        может обогнать старую с большим кол-вом."""
        memory = get_memory_path(agent_dir)
        wiki = memory / "wiki" / "concepts"
        (wiki / "fresh.md").write_text("# Свежая")
        (wiki / "stale.md").write_text("# Устаревшая")

        today = datetime.now().strftime("%Y-%m-%d")
        old_date = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
        stats_dir = memory / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        hits_data = {
            "wiki/concepts/fresh.md": {"hits": 5, "last": today},
            # 10 * 0.9^20 = 10 * 0.1216 = 1.216
            "wiki/concepts/stale.md": {"hits": 10, "last": old_date},
        }
        (stats_dir / "page_hits.json").write_text(json.dumps(hits_data))

        # fresh: 5 * 1.0 = 5.0
        # stale: 10 * 0.9^20 ≈ 1.2
        result = _get_hot_pages(agent_dir, 100)
        # С бюджетом 100 — только одна страница, должна быть fresh
        assert "Свежая" in result


# ── Read Daily Smart tests ──


class TestReadDailySmart:
    def test_short_daily_returned_as_is(self, agent_dir):
        """Если daily помещается в бюджет — возвращаем как есть."""
        memory = get_memory_path(agent_dir)
        daily_dir = memory / "daily"
        today = datetime.now().strftime("%Y-%m-%d")
        daily_path = daily_dir / f"{today}.md"
        daily_path.write_text("# 2026-04-11\n\n**10:00** 👤 Привет\n**10:05** 🤖 Здравствуй\n")

        budget = {"daily_recent": 2000, "daily_summary": 1000}
        result = _read_daily_smart(agent_dir, budget)
        assert "Привет" in result
        assert "Здравствуй" in result

    def test_long_daily_splits(self, agent_dir):
        """Длинный daily разбивается на summary + recent."""
        memory = get_memory_path(agent_dir)
        daily_dir = memory / "daily"
        today = datetime.now().strftime("%Y-%m-%d")
        daily_path = daily_dir / f"{today}.md"

        # Создать daily с множеством записей
        content = f"# {today}\n\n"
        for i in range(50):
            h = 8 + i // 6
            m = (i * 10) % 60
            content += f"\n**{h:02d}:{m:02d}** 👤 Сообщение номер {i} с деталями " + "x" * 100 + "\n"

        daily_path.write_text(content)

        budget = {"daily_recent": 500, "daily_summary": 300}
        result = _read_daily_smart(agent_dir, budget)

        # Должен содержать краткое содержание
        assert "краткое содержание" in result
        # Общий размер должен быть ограничен
        assert len(result) < len(content)

    def test_no_daily_file(self, agent_dir):
        budget = {"daily_recent": 2000, "daily_summary": 1000}
        result = _read_daily_smart(agent_dir, budget)
        assert result == ""

    def test_recent_messages_preserved_full(self, agent_dir):
        """Последние сообщения сохраняются полностью."""
        memory = get_memory_path(agent_dir)
        daily_dir = memory / "daily"
        today = datetime.now().strftime("%Y-%m-%d")
        daily_path = daily_dir / f"{today}.md"

        content = f"# {today}\n\n"
        # Много ранних сообщений
        for i in range(30):
            content += f"\n**08:{i:02d}** 👤 Раннее сообщение {i} " + "y" * 80 + "\n"
        # Несколько последних
        content += "\n**23:50** 👤 Важное последнее сообщение с полным текстом\n"
        content += "\n**23:55** 🤖 Ответ на последнее\n"

        daily_path.write_text(content)

        budget = {"daily_recent": 500, "daily_summary": 200}
        result = _read_daily_smart(agent_dir, budget)

        # Последние сообщения должны быть полностью
        assert "Важное последнее сообщение с полным текстом" in result
        assert "Ответ на последнее" in result


# ── Build Smart Context tests ──


class TestBuildSmartContext:
    def test_includes_profile(self, agent_dir):
        memory = get_memory_path(agent_dir)
        (memory / "profile.md").write_text("# Профиль\nФаундер, 35 лет")

        ctx = build_smart_context(agent_dir)
        assert "Фаундер" in ctx
        assert "Профиль пользователя" in ctx

    def test_includes_index(self, agent_dir):
        memory = get_memory_path(agent_dir)
        (memory / "index.md").write_text("# Каталог\n- Проект X\n- Проект Y")

        ctx = build_smart_context(agent_dir)
        assert "Проект X" in ctx
        assert "Каталог знаний" in ctx

    def test_includes_wiki_search_results(self, agent_dir):
        memory = get_memory_path(agent_dir)
        wiki = memory / "wiki" / "concepts"
        (wiki / "pricing.md").write_text("# Ценообразование\nМодель подписки: базовый план 10$")

        ctx = build_smart_context(agent_dir, user_query="ценообразование подписка")
        assert "Релевантные знания" in ctx
        assert "Ценообразование" in ctx

    def test_includes_daily(self, agent_dir):
        log_message(agent_dir, "user", "Тестовое сообщение для smart context")

        ctx = build_smart_context(agent_dir)
        assert "Тестовое сообщение" in ctx

    def test_includes_hot_pages(self, agent_dir):
        memory = get_memory_path(agent_dir)
        wiki = memory / "wiki" / "concepts"
        (wiki / "team.md").write_text("# Команда\nСписок участников")

        today = datetime.now().strftime("%Y-%m-%d")
        stats_dir = memory / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        hits_data = {"wiki/concepts/team.md": {"hits": 10, "last": today}}
        (stats_dir / "page_hits.json").write_text(json.dumps(hits_data))

        ctx = build_smart_context(agent_dir)
        assert "Часто используемые" in ctx
        assert "Команда" in ctx

    def test_deduplication_wiki_and_hot(self, agent_dir):
        """Wiki search результаты не дублируются в hot pages."""
        memory = get_memory_path(agent_dir)
        wiki = memory / "wiki" / "concepts"
        (wiki / "pricing.md").write_text("# Ценообразование\nМодель подписки")

        # Сделать pricing.md и hot page
        today = datetime.now().strftime("%Y-%m-%d")
        stats_dir = memory / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        hits_data = {"wiki/concepts/pricing.md": {"hits": 20, "last": today}}
        (stats_dir / "page_hits.json").write_text(json.dumps(hits_data))

        ctx = build_smart_context(agent_dir, user_query="ценообразование подписка")
        # Должна быть в "Релевантные знания", но НЕ в "Часто используемые"
        assert "Релевантные знания" in ctx
        # Подсчитать количество вхождений "Ценообразование" — должно быть ровно 1
        assert ctx.count("# Ценообразование") == 1

    def test_empty_if_no_files(self, agent_dir):
        ctx = build_smart_context(agent_dir)
        assert ctx == ""

    def test_custom_budget(self, agent_dir):
        memory = get_memory_path(agent_dir)
        (memory / "profile.md").write_text("x" * 5000)

        custom_budget = {
            "profile": 100,
            "hot_pages": 0,
            "wiki_search": 0,
            "daily_recent": 0,
            "daily_summary": 0,
            "index": 0,
        }
        ctx = build_smart_context(agent_dir, budget=custom_budget)
        # Profile должен быть обрезан до 100 + "..."
        assert "..." in ctx
        assert len(ctx) < 200

    def test_backward_compat_read_context(self, agent_dir):
        """read_context() по-прежнему работает (не сломан)."""
        memory = get_memory_path(agent_dir)
        (memory / "profile.md").write_text("# Профиль\nФаундер")
        (memory / "index.md").write_text("# Каталог\n- Проект")

        ctx = read_context(agent_dir)
        assert "Фаундер" in ctx
        assert "Проект" in ctx

    def test_total_context_within_limit(self, agent_dir):
        """Суммарный контекст не превышает разумный лимит."""
        memory = get_memory_path(agent_dir)
        (memory / "profile.md").write_text("# Профиль\n" + "x" * 3000)
        (memory / "index.md").write_text("# Каталог\n" + "y" * 3000)

        wiki = memory / "wiki" / "concepts"
        for i in range(10):
            (wiki / f"page{i}.md").write_text(f"# Страница {i}\n" + "z" * 1000)

        # Много daily записей
        for i in range(100):
            log_message(agent_dir, "user", f"Сообщение {i} " + "w" * 50)

        # Hot pages
        today = datetime.now().strftime("%Y-%m-%d")
        stats_dir = memory / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        hits_data = {}
        for i in range(10):
            hits_data[f"wiki/concepts/page{i}.md"] = {"hits": 10 - i, "last": today}
        (stats_dir / "page_hits.json").write_text(json.dumps(hits_data))

        ctx = build_smart_context(agent_dir, user_query="страница тест")
        # Суммарный бюджет ~11500 + заголовки секций, допускаем запас
        assert len(ctx) < 15000


# ── Ensure Dirs includes stats ──


class TestEnsureDirsStats:
    def test_creates_stats_dir(self, agent_dir):
        memory = get_memory_path(agent_dir)
        assert (memory / "stats").is_dir()
