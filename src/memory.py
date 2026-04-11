"""
Система памяти: Karpathy LLM Wiki + Obsidian daily notes.

Структура:
  agents/{name}/memory/
    index.md          — Мастер-каталог wiki-страниц
    log.md            — Хронологический лог (append-only)
    profile.md        — Кто пользователь, предпочтения
    daily/YYYY-MM-DD.md  — Ежедневные заметки
    wiki/             — Wiki-страницы (entities, concepts, synthesis)
    raw/files/        — Входящие файлы
    raw/conversations/ — Бэкап диалогов JSONL
    sessions/         — ID текущей сессии Claude CLI
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def get_memory_path(agent_dir: str) -> Path:
    """Получить путь к memory/ для агента."""
    return Path(agent_dir) / "memory"


def ensure_dirs(agent_dir: str) -> None:
    """Создать все необходимые директории памяти."""
    memory = get_memory_path(agent_dir)
    for subdir in [
        "daily",
        "wiki/entities",
        "wiki/concepts",
        "wiki/synthesis",
        "raw/files",
        "raw/conversations",
        "sessions",
        "stats",
    ]:
        (memory / subdir).mkdir(parents=True, exist_ok=True)


def ensure_daily_note(agent_dir: str, date: datetime | None = None) -> Path:
    """Создать daily note если не существует. Вернуть путь."""
    if date is None:
        date = datetime.now()
    memory = get_memory_path(agent_dir)
    daily_dir = memory / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)

    filename = date.strftime("%Y-%m-%d") + ".md"
    path = daily_dir / filename

    if not path.exists():
        header = date.strftime("# %Y-%m-%d %A\n\n")
        path.write_text(header, encoding="utf-8")

    return path


def log_message(
    agent_dir: str,
    role: str,
    content: str,
    files: list[str] | None = None,
    date: datetime | None = None,
) -> None:
    """
    Записать сообщение в daily note + conversations.jsonl.

    Args:
        agent_dir: путь к директории агента (agents/me/)
        role: "user" или "assistant"
        content: текст сообщения
        files: список путей к файлам (опционально)
        date: дата (по умолчанию — сейчас)
    """
    if date is None:
        date = datetime.now()

    ensure_dirs(agent_dir)

    # 1. Запись в daily note
    daily_path = ensure_daily_note(agent_dir, date)
    timestamp = date.strftime("%H:%M")
    prefix = "👤" if role == "user" else "🤖"
    entry = f"\n**{timestamp}** {prefix} {content[:500]}\n"
    if files:
        for f in files:
            entry += f"  📎 {os.path.basename(f)}\n"

    with open(daily_path, "a", encoding="utf-8") as fh:
        fh.write(entry)

    # 2. Запись в conversations.jsonl
    memory = get_memory_path(agent_dir)
    conv_dir = memory / "raw" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    conv_file = conv_dir / f"conversations-{date.strftime('%Y-%m-%d')}.jsonl"

    record = {
        "timestamp": date.isoformat(),
        "role": role,
        "content": content,
    }
    if files:
        record["files"] = files

    with open(conv_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 3. Append в log.md
    log_path = memory / "log.md"
    log_entry = f"- [{date.strftime('%Y-%m-%d %H:%M')}] {role}: {content[:200]}\n"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(log_entry)


STOP_WORDS = {
    # Russian
    "и", "в", "на", "с", "по", "для", "что", "как", "это", "не",
    "а", "но", "или", "из", "к", "от", "до", "за", "о", "об",
    "у", "же", "то", "бы", "ли", "мне", "мой", "мы", "вы", "он",
    "она", "они", "его", "её", "их", "где", "когда", "кто",
    "так", "все", "уже", "ещё", "тоже", "очень", "вот", "есть",
    # English
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "may", "might", "shall",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "or", "but", "if", "then", "than", "no", "not",
    "it", "this", "that", "which", "who", "whom", "what", "how",
    "my", "your", "we", "they",
}

# Максимум символов wiki-контекста в промпте
_WIKI_MAX_TOTAL_CHARS = 4000
# Максимум символов одной wiki-страницы
_WIKI_MAX_PAGE_CHARS = 1500


def _tokenize(text: str) -> list[str]:
    """Разбить текст на токены (lowercase, без стоп-слов)."""
    import re as _re
    words = _re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]+", text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 1]


def search_wiki(agent_dir: str, query: str, max_results: int = 3) -> list[dict]:
    """
    Поиск релевантных wiki-страниц по пользовательскому запросу.

    Стратегия:
    1. Токенизировать запрос -> множество ключевых слов (без стоп-слов)
    2. Для каждой wiki-страницы: рассчитать score (сколько слов запроса встречается)
    3. Бонус: совпадение в заголовке (строка #) — x3
    4. Бонус: совпадение в имени файла — x2
    5. Вернуть top-N страниц с содержимым

    Returns: [{path: str, title: str, content: str, score: float}, ...]
    """
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    memory = get_memory_path(agent_dir)
    wiki_dir = memory / "wiki"
    if not wiki_dir.exists():
        return []

    query_set = set(query_tokens)
    scored: list[dict] = []

    for md_file in wiki_dir.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            continue

        # Извлечь заголовок (первая строка с #)
        title = md_file.stem
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                break

        # Токенизировать содержимое, заголовок, имя файла
        content_tokens = set(_tokenize(content))
        title_tokens = set(_tokenize(title))
        filename_tokens = set(_tokenize(md_file.stem))

        # Подсчёт score
        score = 0.0
        for qt in query_set:
            if qt in content_tokens:
                score += 1.0
            if qt in title_tokens:
                score += 3.0
            if qt in filename_tokens:
                score += 2.0

        if score > 0:
            # Путь относительно memory/ (для совместимости с _get_hot_pages)
            rel_path = str(md_file.relative_to(memory))
            scored.append({
                "path": rel_path,
                "title": title,
                "content": content,
                "score": score,
            })

    # Сортировка по score (убывание), взять top-N
    scored.sort(key=lambda x: x["score"], reverse=True)
    results = scored[:max_results]

    # Обрезать контент слишком длинных страниц
    total_chars = 0
    trimmed: list[dict] = []
    for r in results:
        page_content = r["content"]
        if len(page_content) > _WIKI_MAX_PAGE_CHARS:
            page_content = page_content[:_WIKI_MAX_PAGE_CHARS] + "..."
        if total_chars + len(page_content) > _WIKI_MAX_TOTAL_CHARS:
            remaining = _WIKI_MAX_TOTAL_CHARS - total_chars
            if remaining > 200:
                page_content = page_content[:remaining] + "..."
            else:
                break
        total_chars += len(page_content)
        trimmed.append({**r, "content": page_content})

    # Трекинг: отмечаем использование возвращённых страниц
    for r in trimmed:
        track_page_hit(agent_dir, r["path"])

    return trimmed


def read_context(agent_dir: str, user_query: str = "") -> str:
    """
    Прочитать контекст агента: profile.md + index.md + wiki search.
    Возвращает строку для system prompt.
    """
    memory = get_memory_path(agent_dir)
    parts = []

    # profile.md
    profile_path = memory / "profile.md"
    if profile_path.exists():
        parts.append("## Профиль пользователя\n")
        parts.append(profile_path.read_text(encoding="utf-8"))

    # index.md
    index_path = memory / "index.md"
    if index_path.exists():
        parts.append("\n## Каталог знаний\n")
        parts.append(index_path.read_text(encoding="utf-8"))

    # Сегодняшняя daily note
    today = datetime.now()
    daily_path = memory / "daily" / f"{today.strftime('%Y-%m-%d')}.md"
    if daily_path.exists():
        text = daily_path.read_text(encoding="utf-8")
        # Ограничить до последних ~8000 символов (хватает на ~100 сообщений)
        if len(text) > 8000:
            text = "...(начало дня обрезано)\n" + text[-8000:]
        parts.append("\n## Сегодняшний лог\n")
        parts.append(text)

    # Семантический поиск по wiki
    if user_query:
        wiki_results = search_wiki(agent_dir, user_query, max_results=3)
        if wiki_results:
            parts.append("\n## Релевантные знания из wiki\n")
            for r in wiki_results:
                parts.append(f"### {r['title']} ({r['path']})\n{r['content']}\n")

    return "\n".join(parts)




# ── Smart Context Management ──

CONTEXT_BUDGET = {
    "profile": 2000,       # Профиль — самое важное, всегда полный
    "hot_pages": 3000,     # Часто используемые wiki-страницы
    "wiki_search": 2000,   # Релевантные страницы (из search_wiki)
    "daily_recent": 2000,  # Последние сообщения сегодня
    "daily_summary": 1000, # Краткое содержание ранних сообщений
    "index": 1500,         # Каталог знаний (обрезается если большой)
}


def _read_with_limit(path: Path, limit: int) -> str:
    """Прочитать файл, обрезать до limit символов с '...' если нужно."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def track_page_hit(agent_dir: str, page_path: str) -> None:
    """Записать обращение к wiki-странице в stats/page_hits.json."""
    memory = get_memory_path(agent_dir)
    stats_dir = memory / "stats"
    stats_dir.mkdir(parents=True, exist_ok=True)
    hits_file = stats_dir / "page_hits.json"

    data: dict = {}
    if hits_file.exists():
        try:
            data = json.loads(hits_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    today = datetime.now().strftime("%Y-%m-%d")
    if page_path in data:
        data[page_path]["hits"] = data[page_path].get("hits", 0) + 1
        data[page_path]["last"] = today
    else:
        data[page_path] = {"hits": 1, "last": today}

    hits_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _get_hot_pages(
    agent_dir: str, budget: int, exclude: list[str] | None = None
) -> str:
    """
    Вернуть содержимое самых популярных wiki-страниц в рамках бюджета.

    Score = hits * decay_factor, где decay = 0.9^days_since_last.
    Страницы без обращений 30+ дней считаются 'cold' и исключаются.
    """
    memory = get_memory_path(agent_dir)
    hits_file = memory / "stats" / "page_hits.json"
    if not hits_file.exists():
        return ""

    try:
        data = json.loads(hits_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""

    if not data:
        return ""

    exclude_set = set(exclude or [])
    today = datetime.now()
    scored: list[tuple[str, float]] = []

    for page_path, info in data.items():
        if page_path in exclude_set:
            continue
        hits = info.get("hits", 0)
        last_str = info.get("last", "")
        try:
            last_date = datetime.strptime(last_str, "%Y-%m-%d")
        except ValueError:
            continue

        days_ago = (today - last_date).days
        if days_ago >= 30:
            continue  # cold page

        decay = 0.9 ** days_ago
        score = hits * decay
        scored.append((page_path, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    parts = []
    used = 0
    for page_path, _score in scored:
        full_path = memory / page_path
        if not full_path.exists():
            continue
        try:
            content = full_path.read_text(encoding="utf-8")
        except OSError:
            continue

        if used + len(content) > budget:
            remaining = budget - used
            if remaining > 100:  # только если есть место для осмысленного фрагмента
                parts.append(content[:remaining] + "...")
                used += remaining
            break

        parts.append(content)
        used += len(content)

    return "\n\n".join(parts)


def _read_daily_smart(agent_dir: str, budget: dict) -> str:
    """
    Прочитать сегодняшнюю daily note с умным разделением:
    - Последние N записей — полностью (budget["daily_recent"])
    - Ранние записи — только первая строка каждой (budget["daily_summary"])

    Записи разделяются по паттерну **HH:MM**.
    Если всё помещается в суммарный бюджет — возвращает как есть.
    """
    import re as _re

    memory = get_memory_path(agent_dir)
    today_str = datetime.now().strftime("%Y-%m-%d")
    daily_path = memory / "daily" / f"{today_str}.md"

    if not daily_path.exists():
        return ""

    try:
        text = daily_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    total_budget = budget.get("daily_recent", 2000) + budget.get("daily_summary", 1000)
    if len(text) <= total_budget:
        return text

    # Разбить на записи по паттерну **HH:MM**
    # Каждая запись начинается с \n**HH:MM** или начала файла
    entries = _re.split(r"(?=\n\*\*\d{2}:\d{2}\*\*)", text)
    # Первый элемент — заголовок дня (# YYYY-MM-DD ...)
    header = entries[0] if entries else ""
    message_entries = entries[1:] if len(entries) > 1 else []

    if not message_entries:
        return _read_with_limit(daily_path, total_budget)

    # Определить сколько последних записей помещается в daily_recent
    recent_budget = budget.get("daily_recent", 2000)
    summary_budget = budget.get("daily_summary", 1000)

    recent_entries: list[str] = []
    recent_total = 0
    for entry in reversed(message_entries):
        if recent_total + len(entry) > recent_budget:
            break
        recent_entries.insert(0, entry)
        recent_total += len(entry)

    # Ранние записи (те, что не попали в recent)
    recent_start_idx = len(message_entries) - len(recent_entries)
    early_entries = message_entries[:recent_start_idx]

    # Сжать ранние записи — только первая строка каждой
    summary_parts = []
    summary_total = 0
    for entry in early_entries:
        # Первая непустая строка записи
        lines = entry.strip().split("\n")
        first_line = lines[0] if lines else ""
        if summary_total + len(first_line) + 1 > summary_budget:
            break
        summary_parts.append(first_line)
        summary_total += len(first_line) + 1

    # Собрать результат
    parts = [header.strip()]
    if summary_parts:
        parts.append("\n...(краткое содержание раннего дня)...")
        parts.append("\n".join(summary_parts))
    if recent_entries:
        parts.append("".join(recent_entries))

    return "\n".join(parts)


def build_smart_context(
    agent_dir: str, user_query: str = "", budget: dict | None = None
) -> str:
    """
    Собрать контекст с приоритизацией и символьным бюджетом.

    Приоритеты (от высокого к низкому):
    1. profile.md — кто пользователь
    2. hot pages — часто читаемые wiki-страницы
    3. wiki search — релевантные текущему запросу
    4. Свежие daily — последние сообщения
    5. Краткое содержание ранних daily — сжатые старые сообщения
    6. index.md — каталог знаний
    """
    if budget is None:
        budget = CONTEXT_BUDGET.copy()

    memory = get_memory_path(agent_dir)
    parts = []

    # 1. Profile — приоритет #1, никогда не обрезается (если <2000)
    profile_path = memory / "profile.md"
    profile_text = _read_with_limit(profile_path, budget.get("profile", 2000))
    if profile_text:
        parts.append("## Профиль пользователя\n")
        parts.append(profile_text)

    # 2. Wiki search — релевантные страницы по запросу
    wiki_search_paths: list[str] = []
    if user_query:
        search_results = search_wiki(agent_dir, user_query, max_results=3)
        wiki_search_paths = [r["path"] for r in search_results]
        wiki_budget = budget.get("wiki_search", 2000)
        wiki_parts = []
        wiki_used = 0
        for r in search_results:
            content = r["content"]
            if wiki_used + len(content) > wiki_budget:
                remaining = wiki_budget - wiki_used
                if remaining > 100:
                    wiki_parts.append(content[:remaining] + "...")
                    wiki_used += remaining
                break
            wiki_parts.append(content)
            wiki_used += len(content)

        if wiki_parts:
            parts.append("\n## Релевантные знания\n")
            parts.append("\n\n".join(wiki_parts))

    # 3. Hot pages — часто используемые (исключая уже добавленные из поиска)
    hot_text = _get_hot_pages(
        agent_dir,
        budget.get("hot_pages", 3000),
        exclude=wiki_search_paths,
    )
    if hot_text:
        parts.append("\n## Часто используемые знания\n")
        parts.append(hot_text)

    # 4+5. Daily — свежие + краткое содержание ранних
    daily_text = _read_daily_smart(agent_dir, budget)
    if daily_text:
        parts.append("\n## Сегодняшний лог\n")
        parts.append(daily_text)

    # 6. Index — каталог знаний
    index_path = memory / "index.md"
    index_text = _read_with_limit(index_path, budget.get("index", 1500))
    if index_text:
        parts.append("\n## Каталог знаний\n")
        parts.append(index_text)

    return "\n".join(parts)


def save_session_id(agent_dir: str, session_id: str) -> None:
    """Сохранить ID сессии Claude CLI для --resume."""
    memory = get_memory_path(agent_dir)
    session_dir = memory / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "current_session_id"
    session_file.write_text(session_id, encoding="utf-8")


def get_session_id(agent_dir: str) -> str | None:
    """Прочитать ID текущей сессии. None если нет."""
    memory = get_memory_path(agent_dir)
    session_file = memory / "sessions" / "current_session_id"
    if session_file.exists():
        sid = session_file.read_text(encoding="utf-8").strip()
        return sid if sid else None
    return None


def clear_session_id(agent_dir: str) -> None:
    """Удалить текущую сессию (для /newsession)."""
    memory = get_memory_path(agent_dir)
    session_file = memory / "sessions" / "current_session_id"
    if session_file.exists():
        session_file.unlink()


def archive_old_conversations(agent_dir: str, days: int = 30) -> int:
    """
    Архивировать conversations старше N дней.
    Перемещает в raw/conversations/archive/.
    Возвращает количество архивированных файлов.
    """
    memory = get_memory_path(agent_dir)
    conv_dir = memory / "raw" / "conversations"
    archive_dir = conv_dir / "archive"

    if not conv_dir.exists():
        return 0

    archive_dir.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now() - timedelta(days=days)
    archived = 0

    for f in conv_dir.glob("conversations-*.jsonl"):
        # Извлечь дату из имени файла
        try:
            date_str = f.stem.replace("conversations-", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff:
                shutil.move(str(f), str(archive_dir / f.name))
                archived += 1
        except ValueError:
            continue

    return archived


# ── Group memory ──


def ensure_group_dirs(agent_dir: str, chat_id: int) -> None:
    """Создать директории для группового чата: groups/{chat_id}/daily/, wiki/."""
    memory = get_memory_path(agent_dir)
    group_dir = memory / "groups" / str(chat_id)
    for subdir in ["daily", "wiki"]:
        (group_dir / subdir).mkdir(parents=True, exist_ok=True)


def log_group_message(
    agent_dir: str,
    chat_id: int,
    sender_name: str,
    content: str,
    date: datetime | None = None,
) -> None:
    """
    Записать сообщение группы в groups/{chat_id}/daily/YYYY-MM-DD.md.

    Формат: **HH:MM** 👤 Алексей: текст сообщения
    Записывается КАЖДОЕ сообщение, даже без mention.
    """
    if date is None:
        date = datetime.now()

    ensure_group_dirs(agent_dir, chat_id)
    memory = get_memory_path(agent_dir)
    daily_dir = memory / "groups" / str(chat_id) / "daily"

    filename = date.strftime("%Y-%m-%d") + ".md"
    path = daily_dir / filename

    if not path.exists():
        header = date.strftime("# %Y-%m-%d %A\n\n")
        path.write_text(header, encoding="utf-8")

    timestamp = date.strftime("%H:%M")
    entry = f"**{timestamp}** 👤 {sender_name}: {content[:500]}\n"

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry)


def read_group_context(agent_dir: str, chat_id: int) -> str:
    """
    Прочитать контекст группы для system prompt:
    groups/{chat_id}/context.md + daily note (последние ~8000 символов).
    """
    memory = get_memory_path(agent_dir)
    group_dir = memory / "groups" / str(chat_id)
    parts = []

    # context.md — описание группы
    context_path = group_dir / "context.md"
    if context_path.exists():
        parts.append("## Контекст группы\n")
        parts.append(context_path.read_text(encoding="utf-8"))

    # Сегодняшний daily note группы
    today = datetime.now()
    daily_path = group_dir / "daily" / f"{today.strftime('%Y-%m-%d')}.md"
    if daily_path.exists():
        text = daily_path.read_text(encoding="utf-8")
        if len(text) > 8000:
            text = "...(начало дня обрезано)\n" + text[-8000:]
        parts.append("\n## Лог группы за сегодня\n")
        parts.append(text)

    return "\n".join(parts)


def is_group_onboarding_needed(agent_dir: str, chat_id: int) -> bool:
    """Проверить, есть ли context.md для группы. True = нужен онбординг."""
    memory = get_memory_path(agent_dir)
    context_path = memory / "groups" / str(chat_id) / "context.md"
    return not context_path.exists()


def create_group_context(
    agent_dir: str,
    chat_id: int,
    chat_title: str,
    chat_type: str,
) -> None:
    """Создать context.md для новой группы с шаблоном."""
    ensure_group_dirs(agent_dir, chat_id)
    memory = get_memory_path(agent_dir)
    context_path = memory / "groups" / str(chat_id) / "context.md"

    today = datetime.now().strftime("%Y-%m-%d")
    template = (
        f"# Группа: {chat_title}\n"
        f"- Chat ID: {chat_id}\n"
        f"- Тип: {chat_type}\n"
        f"- Добавлен: {today}\n"
        f"- Участники: [заполнится автоматически]\n"
        f"- Тема: [определится из контекста]\n"
    )
    context_path.write_text(template, encoding="utf-8")


def get_group_setting(agent_dir: str, chat_id: int, key: str):
    """Прочитать настройку группы из groups/{chat_id}/settings.json."""
    memory = get_memory_path(agent_dir)
    settings_path = memory / "groups" / str(chat_id) / "settings.json"
    if not settings_path.exists():
        return None
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return data.get(key)
    except (json.JSONDecodeError, OSError):
        return None


def set_group_setting(agent_dir: str, chat_id: int, key: str, value) -> None:
    """Записать настройку группы в groups/{chat_id}/settings.json."""
    ensure_group_dirs(agent_dir, chat_id)
    memory = get_memory_path(agent_dir)
    settings_path = memory / "groups" / str(chat_id) / "settings.json"

    data = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    settings_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def update_group_rules(agent_dir: str, chat_id: int, rules: str) -> None:
    """
    Добавить/обновить правила от владельца в context.md группы.

    Добавляет секцию '## Правила от владельца' в конец context.md.
    При повторном вызове — заменяет старые правила новыми.
    """
    memory = get_memory_path(agent_dir)
    context_path = memory / "groups" / str(chat_id) / "context.md"
    if not context_path.exists():
        return

    content = context_path.read_text(encoding="utf-8")

    # Убрать старую секцию правил если есть
    marker = "\n## Правила от владельца\n"
    if marker in content:
        content = content[: content.index(marker)]

    content = content.rstrip() + f"\n\n## Правила от владельца\n{rules}\n"
    context_path.write_text(content, encoding="utf-8")


def is_onboarding_needed(agent_dir: str) -> bool:
    """Проверить, нужен ли онбординг (profile.md ещё не заполнен)."""
    memory = get_memory_path(agent_dir)
    profile_path = memory / "profile.md"
    if not profile_path.exists():
        return True
    content = profile_path.read_text(encoding="utf-8")
    return "[заполни]" in content


def mark_onboarding_done(agent_dir: str) -> None:
    """Пометить что онбординг пройден (убрать плейсхолдеры)."""
    memory = get_memory_path(agent_dir)
    flag = memory / "sessions" / ".onboarding_done"
    flag.write_text("done", encoding="utf-8")


def is_onboarding_done(agent_dir: str) -> bool:
    """Проверить флаг завершения онбординга."""
    memory = get_memory_path(agent_dir)
    flag = memory / "sessions" / ".onboarding_done"
    return flag.exists()


def get_setting(agent_dir: str, key: str) -> str | None:
    """
    Прочитать настройку из settings.json.

    Claude пишет этот файл через Write tool, Python читает.
    Например: deepgram_api_key, language, timezone.
    """
    memory = get_memory_path(agent_dir)
    settings_path = memory / "settings.json"
    if not settings_path.exists():
        return None
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return data.get(key)
    except (json.JSONDecodeError, OSError):
        return None


def set_setting(agent_dir: str, key: str, value: str) -> None:
    """
    Записать настройку в settings.json.

    Сохраняет существующие настройки, обновляет только указанный ключ.
    """
    memory = get_memory_path(agent_dir)
    settings_path = memory / "settings.json"

    data = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    data[key] = value
    settings_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_recent_messages(
    agent_dir: str, limit: int = 50
) -> list[dict]:
    """Получить последние N сообщений из conversations.jsonl."""
    memory = get_memory_path(agent_dir)
    conv_dir = memory / "raw" / "conversations"

    if not conv_dir.exists():
        return []

    # Собрать все jsonl файлы, отсортировать по дате (новые первые)
    files = sorted(conv_dir.glob("conversations-*.jsonl"), reverse=True)

    messages = []
    for f in files:
        if len(messages) >= limit:
            break
        try:
            lines = f.read_text(encoding="utf-8").strip().split("\n")
            for line in reversed(lines):
                if len(messages) >= limit:
                    break
                if line.strip():
                    messages.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            continue

    # Вернуть в хронологическом порядке
    messages.reverse()
    return messages


# ── Git-backed memory ──


def _run_git(memory_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Выполнить git-команду в директории памяти."""
    return subprocess.run(
        ["git", *args],
        cwd=str(memory_path),
        capture_output=True,
        text=True,
        timeout=30,
    )


def git_init(agent_dir: str) -> bool:
    """
    Инициализировать git-репозиторий в memory/ если ещё нет.

    Вызывается при старте агента. Создаёт .gitignore для sessions/.
    """
    memory = get_memory_path(agent_dir)

    if (memory / ".git").exists():
        return True

    try:
        result = _run_git(memory, "init")
        if result.returncode != 0:
            logger.error(f"git init failed: {result.stderr}")
            return False

        # Настроить user для коммитов
        agent_name = Path(agent_dir).name
        _run_git(memory, "config", "user.name", f"agent-{agent_name}")
        _run_git(memory, "config", "user.email", f"{agent_name}@my-claude-bot.local")

        # .gitignore: не трекать сессии, raw conversations и статистику
        gitignore = memory / ".gitignore"
        gitignore.write_text(
            "sessions/\nraw/conversations/\nstats/\n", encoding="utf-8"
        )

        # Начальный коммит
        _run_git(memory, "add", "-A")
        _run_git(memory, "commit", "-m", "Initial memory state")

        logger.info(f"Git инициализирован в {memory}")
        return True
    except Exception as e:
        logger.error(f"git_init error: {e}")
        return False


def git_commit(agent_dir: str, message: str | None = None) -> bool:
    """
    Закоммитить все изменения в memory/.

    Args:
        agent_dir: путь к директории агента
        message: сообщение коммита (авто-генерируется если не указано)

    Returns:
        True если коммит создан, False если нечего коммитить или ошибка
    """
    memory = get_memory_path(agent_dir)

    if not (memory / ".git").exists():
        if not git_init(agent_dir):
            return False

    try:
        # Проверить есть ли изменения
        status = _run_git(memory, "status", "--porcelain")
        if not status.stdout.strip():
            return False  # Нечего коммитить

        # Добавить все изменения
        _run_git(memory, "add", "-A")

        # Коммит
        if not message:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            message = f"Memory update {now}"

        result = _run_git(memory, "commit", "-m", message)
        if result.returncode != 0:
            logger.error(f"git commit failed: {result.stderr}")
            return False

        logger.info(f"Memory committed: {message}")
        return True
    except Exception as e:
        logger.error(f"git_commit error: {e}")
        return False


def git_log(agent_dir: str, limit: int = 10) -> list[dict]:
    """
    Получить историю коммитов памяти.

    Returns:
        Список словарей с ключами: hash, date, message
    """
    memory = get_memory_path(agent_dir)

    if not (memory / ".git").exists():
        return []

    try:
        result = _run_git(
            memory,
            "log",
            f"--max-count={limit}",
            "--format=%H|%ai|%s",
        )
        if result.returncode != 0:
            return []

        entries = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                entries.append(
                    {
                        "hash": parts[0][:8],
                        "date": parts[1][:16],
                        "message": parts[2],
                    }
                )
        return entries
    except Exception as e:
        logger.error(f"git_log error: {e}")
        return []


def git_restore(agent_dir: str, commit_hash: str | None = None) -> bool:
    """
    Откатить память к предыдущему состоянию.

    Args:
        agent_dir: путь к директории агента
        commit_hash: хэш коммита для отката (None = предыдущий коммит)

    Returns:
        True если откат успешен
    """
    memory = get_memory_path(agent_dir)

    if not (memory / ".git").exists():
        return False

    try:
        target = commit_hash or "HEAD~1"

        # Сначала сохранить текущее состояние
        git_commit(agent_dir, "Pre-restore snapshot")

        # Откатить файлы (но не историю)
        result = _run_git(memory, "checkout", target, "--", ".")
        if result.returncode != 0:
            logger.error(f"git restore failed: {result.stderr}")
            return False

        # Закоммитить откат
        _run_git(memory, "add", "-A")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        _run_git(
            memory,
            "commit",
            "-m",
            f"Restored to {target} at {now}",
        )

        logger.info(f"Memory restored to {target}")
        return True
    except Exception as e:
        logger.error(f"git_restore error: {e}")
        return False
