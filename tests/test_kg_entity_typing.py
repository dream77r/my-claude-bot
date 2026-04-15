"""Тесты типизации entity (Этап 3 KG_WIKI_PLAN.md)."""

from datetime import datetime
from pathlib import Path

import pytest

from src.knowledge_graph import (
    _ENTITY_TYPE_TO_FOLDER,
    _ensure_entity_page,
    _entity_folder,
    _normalize_entity_type,
    _safe_filename,
)


def test_all_nine_types_have_folders():
    expected = {
        "Person", "Company", "Project", "Decision", "Idea",
        "Event", "Topic", "Claim", "Document",
    }
    assert set(_ENTITY_TYPE_TO_FOLDER.keys()) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Person", "Person"),
        ("person", "Person"),
        ("PERSON", "Person"),
        ("company", "Company"),
        ("organization", "Company"),
        ("concept", "Idea"),
        ("tool", "Document"),
        ("product", "Project"),
        ("", "Topic"),
        (None, "Topic"),
        ("совершенно_неизвестный", "Topic"),
    ],
)
def test_normalize_entity_type(raw, expected):
    assert _normalize_entity_type(raw) == expected


def test_entity_folder_mapping():
    assert _entity_folder("Person") == "people"
    assert _entity_folder("Decision") == "decisions"
    assert _entity_folder("неизвестно") == "topics"


def test_safe_filename_strips_unsafe_chars():
    assert _safe_filename("Acme/Corp") == "Acme_Corp"
    assert _safe_filename("foo:bar?baz") == "foo_bar_baz"
    assert _safe_filename("  ") == "unnamed"
    assert _safe_filename("Иван Петров").startswith("Иван")


def test_ensure_entity_page_creates_in_correct_folder(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    page = _ensure_entity_page(mem, "Иван", "Person", "2026-04-15")
    assert page.exists()
    assert page.parent.name == "people"
    text = page.read_text(encoding="utf-8")
    assert "type: Person" in text
    assert "created: 2026-04-15" in text
    assert "last_seen: 2026-04-15" in text
    assert "# Иван" in text
    assert "- 2026-04-15" in text


def test_ensure_entity_page_idempotent_on_same_day(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    p1 = _ensure_entity_page(mem, "Acme", "Company", "2026-04-15")
    text1 = p1.read_text(encoding="utf-8")
    p2 = _ensure_entity_page(mem, "Acme", "Company", "2026-04-15")
    text2 = p2.read_text(encoding="utf-8")
    assert text1 == text2
    # Только одно упоминание даты
    assert text2.count("- 2026-04-15") == 1


def test_ensure_entity_page_updates_last_seen_on_new_day(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    _ensure_entity_page(mem, "Иван", "Person", "2026-04-15")
    page = _ensure_entity_page(mem, "Иван", "Person", "2026-04-18")
    text = page.read_text(encoding="utf-8")
    assert "last_seen: 2026-04-18" in text
    assert "last_seen: 2026-04-15" not in text
    # Обе даты в упоминаниях
    assert "- 2026-04-15" in text
    assert "- 2026-04-18" in text


def test_ensure_entity_page_handles_all_nine_types(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    type_to_expected_folder = {
        "Person": "people",
        "Company": "companies",
        "Project": "projects",
        "Decision": "decisions",
        "Idea": "ideas",
        "Event": "events",
        "Topic": "topics",
        "Claim": "claims",
        "Document": "documents",
    }
    for t, folder in type_to_expected_folder.items():
        page = _ensure_entity_page(mem, f"Test{t}", t, "2026-04-15")
        assert page.parent.name == folder
        assert f"type: {t}" in page.read_text(encoding="utf-8")


def test_ensure_entity_page_cross_folder_no_duplicate(tmp_path):
    """
    Если entity уже есть в projects/, повторный вызов с type=Topic должен
    обновить её in-place, а не создать дубликат в topics/.
    """
    mem = tmp_path / "memory"
    mem.mkdir()
    original = _ensure_entity_page(mem, "Phase5", "Project", "2026-04-10")
    assert original.parent.name == "projects"

    again = _ensure_entity_page(mem, "Phase5", "Topic", "2026-04-15")
    assert again == original  # тот же файл
    # Дубликата в topics/ не создано
    assert not (mem / "wiki" / "topics" / "Phase5.md").exists()
    # last_seen обновился
    text = again.read_text(encoding="utf-8")
    assert "last_seen: 2026-04-15" in text
    # Оригинальный тип сохранён
    assert "type: Project" in text


def test_ensure_entity_page_cross_folder_finds_legacy(tmp_path):
    """Entity в legacy-папке wiki/entities/ тоже обнаруживается."""
    mem = tmp_path / "memory"
    (mem / "wiki" / "entities").mkdir(parents=True)
    legacy_page = mem / "wiki" / "entities" / "OldThing.md"
    legacy_page.write_text(
        "---\ntype: Topic\nlast_seen: 2026-04-01\n---\n\n# OldThing\n",
        encoding="utf-8",
    )
    result = _ensure_entity_page(mem, "OldThing", "Topic", "2026-04-15")
    assert result == legacy_page
    assert "last_seen: 2026-04-15" in result.read_text(encoding="utf-8")
    # Новой страницы в topics/ не создано
    assert not (mem / "wiki" / "topics" / "OldThing.md").exists()


def test_ensure_entity_page_creates_new_when_nothing_exists(tmp_path):
    """Если страницы нет нигде — создаётся в папке согласно типу."""
    mem = tmp_path / "memory"
    mem.mkdir()
    page = _ensure_entity_page(mem, "NewThing", "Topic", "2026-04-15")
    assert page.parent.name == "topics"
    assert page.exists()


def test_wiki_search_picks_up_frontmatter_type(tmp_path):
    """wiki_search должен брать тип из frontmatter, а не только из папки."""
    from src.wiki_search import search

    agent = tmp_path / "agents" / "me"
    mem = agent / "memory"
    (mem / "wiki" / "people").mkdir(parents=True)
    (mem / "wiki" / "people" / "Иван.md").write_text(
        "---\ntype: Person\n---\n\n# Иван\n\nFounder Phase5.\n",
        encoding="utf-8",
    )
    hits = search("Phase5", str(agent))
    assert len(hits) >= 1
    assert hits[0].page_type == "Person"
