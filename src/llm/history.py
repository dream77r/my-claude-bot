"""
Реплей истории диалога для CLI-бэкендов без нативного resume.

Бот сам пишет каждое сообщение в memory/raw/conversations/conversations-*.jsonl
(см. memory.log_message). Для не-Claude бэкендов непрерывность диалога
обеспечиваем, подкладывая последние N сообщений транскриптом в промпт.

Важно: telegram-мост делает early-persist — текущее сообщение пользователя уже
записано в jsonl ДО вызова агента. Поэтому хвостовая запись с role="user",
совпадающая с текущим сообщением, отбрасывается.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_LIMIT = 30
MAX_DAYS_BACK = 7
MAX_CONTENT_CHARS = 4000  # обрезка одного сообщения, чтобы история не раздулась


def _conversation_files(memory_dir: Path) -> list[Path]:
    conv_dir = memory_dir / "raw" / "conversations"
    if not conv_dir.exists():
        return []
    files: list[Path] = []
    today = datetime.now()
    for i in range(MAX_DAYS_BACK + 1):
        day = today - timedelta(days=i)
        f = conv_dir / f"conversations-{day.strftime('%Y-%m-%d')}.jsonl"
        if f.exists():
            files.append(f)
    return files  # сегодня первым — читаем в обратном хронологическом порядке


def _read_records(memory_dir: Path) -> list[dict]:
    records: list[dict] = []
    # Файлы идут сегодня-первым — читаем в обратном порядке, чтобы
    # записи складывались хронологически (старые → новые) и хвост
    # records[-limit:] брал действительно последние сообщения.
    for f in reversed(_conversation_files(memory_dir)):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("role") in ("user", "assistant") and rec.get("content"):
                    records.append(rec)
        except OSError:
            continue
    return records


def replay(
    memory_dir: str | Path,
    limit: int = DEFAULT_LIMIT,
    current_message: str | None = None,
) -> str:
    """
    Собрать транскрипт последних сообщений для вставки в промпт.

    Args:
        memory_dir: путь к директории памяти (memory/ или users/{id}).
        limit: сколько последних сообщений взять.
        current_message: текущее сообщение пользователя — если оно уже в логе
            (early-persist), исключается из транскрипта.

    Returns:
        Транскрипт или пустая строка, если истории нет.
    """
    records = _read_records(Path(memory_dir))
    if not records:
        return ""

    # Отбросить хвостовые записи текущего сообщения (early-persist).
    if current_message:
        while (
            records
            and records[-1].get("role") == "user"
            and records[-1].get("content") == current_message
        ):
            records.pop()

    if not records:
        return ""

    tail = records[-limit:]
    lines: list[str] = []
    for rec in tail:
        role = rec["role"]
        who = "Пользователь" if role == "user" else "Ассистент"
        content = str(rec["content"])
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + " …"
        lines.append(f"{who}: {content}")
    return "\n\n".join(lines)
