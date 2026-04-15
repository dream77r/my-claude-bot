"""
Wiki Search — детерминированный поиск по wiki/ + графу.

BM25-lite по entity/concept/synthesis-страницам, BFS по graph.json для
расширения соседями, извлечение цитат из daily-логов. Без внешних
зависимостей. Используется навыком `wiki-search` (см. KG_WIKI_PLAN.md, этап 2).

Запуск как CLI:
    python3 -m src.wiki_search --agent agents/me --query "Phase 5"
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import memory

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[\wа-яА-ЯёЁ]+", re.UNICODE)
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$", re.MULTILINE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Лёгкий парсер YAML frontmatter (только плоские строковые поля)."""
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return {}
    fields: dict[str, str] = {}
    for fm in _FRONTMATTER_FIELD_RE.finditer(match.group(1)):
        fields[fm.group(1)] = fm.group(2).strip().strip("\"'")
    return fields


@dataclass
class Hit:
    name: str
    path: str
    score: float
    page_type: str
    snippet: str = ""
    quote: str = ""
    quote_date: str = ""
    neighbors: list[str] = field(default_factory=list)


def _collect_pages(memory_path: Path) -> list[dict]:
    wiki = memory_path / "wiki"
    if not wiki.exists():
        return []
    pages: list[dict] = []
    for md in wiki.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = md.relative_to(memory_path)
        # frontmatter type важнее папки (Этап 3 типизации). Fallback на папку.
        fm = _parse_frontmatter(text)
        if fm.get("type"):
            page_type = fm["type"]
        else:
            page_type = rel.parts[1] if len(rel.parts) >= 3 else "wiki"
        # Имя страницы (basename без .md) бустим x3 — это её title
        tokens = _tokenize(md.stem) * 3 + _tokenize(text)
        pages.append({
            "name": md.stem,
            "path": str(rel),
            "page_type": page_type,
            "text": text,
            "tokens": tokens,
        })
    return pages


def _bm25_scores(
    pages: list[dict],
    query_tokens: list[str],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    if not pages or not query_tokens:
        return [0.0] * len(pages)

    doc_lens = [len(p["tokens"]) for p in pages]
    n = len(pages)
    avgdl = sum(doc_lens) / n if n else 0.0

    df: Counter[str] = Counter()
    for p in pages:
        for tok in set(p["tokens"]):
            df[tok] += 1

    tf_per_doc = [Counter(p["tokens"]) for p in pages]

    scores = [0.0] * n
    for qt in query_tokens:
        if qt not in df:
            continue
        idf = math.log((n - df[qt] + 0.5) / (df[qt] + 0.5) + 1)
        for i, tf in enumerate(tf_per_doc):
            f = tf.get(qt, 0)
            if f == 0:
                continue
            denom = (
                f + k1 * (1 - b + b * (doc_lens[i] / avgdl if avgdl else 1))
            )
            scores[i] += idf * (f * (k1 + 1)) / denom

    return scores


def _make_snippet(text: str, query_tokens: list[str], window: int = 200) -> str:
    if not text:
        return ""
    lower = text.lower()
    for qt in query_tokens:
        idx = lower.find(qt)
        if idx >= 0:
            start = max(0, idx - window // 2)
            end = min(len(text), idx + window // 2)
            return text[start:end].strip()
    return text[:window].strip()


def search(query: str, agent_dir: str, top_k: int = 5) -> list[Hit]:
    """BM25-поиск по wiki/. Возвращает топ-K страниц."""
    memory_path = memory.get_memory_path(agent_dir)
    pages = _collect_pages(memory_path)
    if not pages:
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    scores = _bm25_scores(pages, query_tokens)
    ranked = sorted(zip(pages, scores), key=lambda x: x[1], reverse=True)

    hits: list[Hit] = []
    for page, score in ranked[:top_k]:
        if score <= 0:
            continue
        hits.append(Hit(
            name=page["name"],
            path=page["path"],
            score=score,
            page_type=page["page_type"],
            snippet=_make_snippet(page["text"], query_tokens),
        ))
    return hits


def bfs(
    graph: dict,
    start: str,
    depth: int = 1,
    include_superseded: bool = False,
) -> list[str]:
    """
    BFS по graph.json от стартовой entity. Регистр игнорируется.

    По умолчанию superseded-рёбра игнорируются (граф = текущая правда).
    Передай include_superseded=True для исторического обзора.
    """
    edges = graph.get("edges", []) or []
    adj: dict[str, set[str]] = defaultdict(set)
    name_norm: dict[str, str] = {}  # lower → canonical (последнее встреченное)

    for e in edges:
        if not include_superseded and e.get("superseded_by"):
            continue
        a = e.get("from", "")
        c = e.get("to", "")
        if not a or not c:
            continue
        a_l, c_l = a.lower(), c.lower()
        adj[a_l].add(c_l)
        adj[c_l].add(a_l)
        name_norm[a_l] = a
        name_norm[c_l] = c

    start_l = start.lower()
    if start_l not in adj:
        return []

    visited = {start_l}
    frontier = {start_l}
    for _ in range(depth):
        next_frontier: set[str] = set()
        for node in frontier:
            for nb in adj[node]:
                if nb not in visited:
                    visited.add(nb)
                    next_frontier.add(nb)
        frontier = next_frontier
        if not frontier:
            break

    visited.discard(start_l)
    return sorted({name_norm.get(n, n) for n in visited})


def quote_from_daily(
    entity_name: str,
    date: str,
    agent_dir: str,
    context_lines: int = 2,
) -> str:
    """Найти первое упоминание entity в daily/<date>.md, вернуть с контекстом."""
    if not entity_name or not date:
        return ""
    memory_path = memory.get_memory_path(agent_dir)
    daily_path = memory_path / "daily" / f"{date}.md"
    if not daily_path.exists():
        return ""
    try:
        text = daily_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    name_lower = entity_name.lower()
    for i, line in enumerate(lines):
        if name_lower in line.lower():
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            return "\n".join(lines[start:end]).strip()
    return ""


def _load_graph(agent_dir: str) -> dict:
    memory_path = memory.get_memory_path(agent_dir)
    graph_path = memory_path / "graph.json"
    if graph_path.exists():
        try:
            return json.loads(graph_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"edges": []}


def _last_seen_date(graph: dict, entity_name: str) -> str:
    name_lower = entity_name.lower()
    last = ""
    for e in graph.get("edges", []) or []:
        from_l = e.get("from", "").lower()
        to_l = e.get("to", "").lower()
        if name_lower == from_l or name_lower == to_l:
            d = e.get("last_seen", "") or e.get("date", "")
            if d and d > last:
                last = d
    return last


def recall(query: str, agent_dir: str, top_k: int = 5) -> dict:
    """
    Высокоуровневый recall: BM25-поиск + BFS-расширение + цитаты.

    Возвращает структурированный dict, который master-агент потом
    переформатирует в человеческий ответ.
    """
    hits = search(query, agent_dir, top_k=top_k)
    graph = _load_graph(agent_dir)

    seen_names = {h.name.lower() for h in hits}
    extra_neighbors: list[str] = []
    for h in hits[:3]:
        ns = bfs(graph, h.name, depth=1)
        h.neighbors = ns
        for n in ns:
            if n.lower() not in seen_names:
                extra_neighbors.append(n)
                seen_names.add(n.lower())

    for h in hits:
        last_seen = _last_seen_date(graph, h.name)
        if last_seen:
            h.quote = quote_from_daily(h.name, last_seen, agent_dir)
            h.quote_date = last_seen

    return {
        "query": query,
        "hits": [
            {
                "name": h.name,
                "path": h.path,
                "type": h.page_type,
                "score": round(h.score, 3),
                "snippet": h.snippet,
                "neighbors": h.neighbors,
                "quote": h.quote,
                "quote_date": h.quote_date,
            }
            for h in hits
        ],
        "extra_neighbors": extra_neighbors[:5],
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wiki Search — BM25 + BFS поиск по локальной памяти агента.",
    )
    parser.add_argument("--agent", required=True, help="Путь к директории агента")
    parser.add_argument("--query", required=True, help="Поисковый запрос")
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args(argv)

    result = recall(args.query, args.agent, top_k=args.top)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
