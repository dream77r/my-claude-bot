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
