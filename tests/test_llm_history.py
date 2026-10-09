"""Тесты реплея истории для CLI-бэкендов (src.llm.history.replay).

Фикстуры пишутся в raw/conversations/conversations-YYYY-MM-DD.jsonl
относительно datetime.now() — тесты дата-стабильны.
"""

import json
from datetime import datetime, timedelta

from src.llm import history


def _write_jsonl(memory_dir, day_offset, records):
    """Записать фикстуру conversations-<today-offset>.jsonl.

    records — список dict (сериализуем в JSON) или сырых строк.
    """
    conv = memory_dir / "raw" / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    day = (datetime.now() - timedelta(days=day_offset)).strftime("%Y-%m-%d")
    path = conv / f"conversations-{day}.jsonl"
    lines = [
        r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        for r in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _rec(role, content):
    return {"role": role, "content": content, "timestamp": "2026-10-09T10:00:00"}


def test_replay_returns_last_n_records(tmp_path):
    _write_jsonl(tmp_path, 0, [
        _rec("user", "первый"),
        _rec("assistant", "ответ 1"),
        _rec("user", "второй"),
        _rec("assistant", "ответ 2"),
    ])
    transcript = history.replay(tmp_path, limit=2)
    # Старые записи отброшены — остался хвост
    assert "второй" in transcript
    assert "ответ 2" in transcript
    assert "первый" not in transcript
    assert "ответ 1" not in transcript


def test_replay_roles_rendered_in_order(tmp_path):
    _write_jsonl(tmp_path, 0, [
        _rec("user", "вопрос"),
        _rec("assistant", "ответ"),
    ])
    transcript = history.replay(tmp_path)
    assert transcript == "Пользователь: вопрос\n\nАссистент: ответ"


def test_replay_multi_day_reads_in_order(tmp_path):
    """Сначала вчерашний файл целиком, потом сегодняшний."""
    _write_jsonl(tmp_path, 1, [
        _rec("user", "вчерашний вопрос"),
        _rec("assistant", "вчерашний ответ"),
    ])
    _write_jsonl(tmp_path, 0, [
        _rec("user", "сегодняшний вопрос"),
    ])
    transcript = history.replay(tmp_path)
    assert transcript == (
        "Пользователь: вчерашний вопрос\n\n"
        "Ассистент: вчерашний ответ\n\n"
        "Пользователь: сегодняшний вопрос"
    )


def test_replay_excludes_trailing_current_message(tmp_path):
    """Early-persist: хвостовая user-запись, совпадающая с текущим
    сообщением, отбрасывается."""
    _write_jsonl(tmp_path, 0, [
        _rec("user", "предыдущий вопрос"),
        _rec("assistant", "ответ"),
        _rec("user", "текущий вопрос"),
    ])
    transcript = history.replay(tmp_path, current_message="текущий вопрос")
    assert "текущий вопрос" not in transcript
    assert "предыдущий вопрос" in transcript


def test_replay_keeps_trailing_user_when_different(tmp_path):
    """Хвостовая user-запись, НЕ совпадающая с current_message, остаётся."""
    _write_jsonl(tmp_path, 0, [
        _rec("user", "вопрос"),
        _rec("assistant", "ответ"),
        _rec("user", "другой вопрос"),
    ])
    transcript = history.replay(tmp_path, current_message="текущий вопрос")
    assert "другой вопрос" in transcript


def test_replay_skips_broken_lines_and_foreign_roles(tmp_path):
    """Битый JSON и роли вне user/assistant пропускаются без падения."""
    _write_jsonl(tmp_path, 0, [
        "{not valid json",
        json.dumps({"role": "system", "content": "системная строка"}),
        json.dumps({"role": "user", "content": ""}),  # пустой контент
        json.dumps(["не", "dict"]),
        _rec("user", "нормальный вопрос"),
        _rec("assistant", "нормальный ответ"),
    ])
    transcript = history.replay(tmp_path)
    assert transcript == "Пользователь: нормальный вопрос\n\nАссистент: нормальный ответ"


def test_replay_missing_dir_returns_empty(tmp_path):
    assert history.replay(tmp_path / "nonexistent") == ""


def test_replay_empty_after_current_exclusion_returns_empty(tmp_path):
    """В логе только само текущее сообщение — транскрипта нет."""
    _write_jsonl(tmp_path, 0, [_rec("user", "текущий вопрос")])
    assert history.replay(tmp_path, current_message="текущий вопрос") == ""


def test_replay_truncates_overlong_content(tmp_path):
    long_content = "x" * (history.MAX_CONTENT_CHARS + 1000)
    _write_jsonl(tmp_path, 0, [_rec("assistant", long_content)])
    transcript = history.replay(tmp_path)
    expected = "x" * history.MAX_CONTENT_CHARS + " …"
    assert f"Ассистент: {expected}" in transcript
    assert long_content not in transcript
