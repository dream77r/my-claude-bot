"""
Wiki Lint — ночные проверки целостности графа и wiki-страниц.

Этап 5 KG_WIKI_PLAN.md. Запускается после KG-цикла. Минимум 5 проверок:
1. Entity с именами из инфраструктурного блок-листа (последняя линия защиты,
   если фильтр в `_extract_user_content` или промпт-блоклист пропустили).
2. Orphan entity-страницы (нет ни одного edge в графе).
3. Висячие edges (endpoint не существует как entity-страница).
4. Дубликаты (одна сущность под двумя именами — case-insensitive).
5. Противоречивые exclusive-edges без supersession.

Результат пишется в `wiki/.lint_report.md`. Выводится из nightly-цикла.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import memory
from .knowledge_graph import _EXCLUSIVE_LINK_TYPES

logger = logging.getLogger(__name__)


# Список запрещённых имён — синхронизирован с промптом kg_level1_links.md.
# Lower-case подстроки.
_INFRA_BLOCKLIST = [
    "smarttrigger",
    "smart trigger",
    "smart_heartbeat",
    "heartbeat",
    "deadline_check",
    "morning_briefing",
    "evening_digest",
    "news_monitor",
    "knowledge_graph",
    "kg_level",
    "dispatcher",
    "fleetbus",
    "agent.yaml",
    "graph.json",
    "log.md",
    "profile.md",
    "kg level",
    "dream_cycle",
    "dream_phase",
    "automated deadline management",
    "information access",
    "notification system",
]

# Точные совпадения (имена, которые сами по себе являются инфраструктурой).
# ВАЖНО: сюда НЕ добавляем обычные слова, которые могут быть настоящими
# именами сущностей пользователя (dream — это может быть никнейм человека).
_INFRA_BLOCKLIST_EXACT = {
    "wiki", "memory", "daily", "summaries", "bus",
    "интеграция инструментов", "интеграция-инструментов",
}


@dataclass
class LintIssue:
    code: str
    severity: str  # "error" | "warning"
    message: str
    where: str = ""


@dataclass
class LintReport:
    blocklist_hits: list[LintIssue] = field(default_factory=list)
    orphans: list[LintIssue] = field(default_factory=list)
    dangling_edges: list[LintIssue] = field(default_factory=list)
    duplicates: list[LintIssue] = field(default_factory=list)
    contradictions: list[LintIssue] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(
            len(v) for v in (
                self.blocklist_hits, self.orphans, self.dangling_edges,
                self.duplicates, self.contradictions,
            )
        )

    @property
    def errors(self) -> int:
        return sum(
            1
            for bucket in (
                self.blocklist_hits, self.orphans, self.dangling_edges,
                self.duplicates, self.contradictions,
            )
            for it in bucket
            if it.severity == "error"
        )

    def to_dict(self) -> dict:
        return {
            "blocklist_hits": [asdict(i) for i in self.blocklist_hits],
            "orphans": [asdict(i) for i in self.orphans],
            "dangling_edges": [asdict(i) for i in self.dangling_edges],
            "duplicates": [asdict(i) for i in self.duplicates],
            "contradictions": [asdict(i) for i in self.contradictions],
            "total": self.total,
            "errors": self.errors,
        }


def _name_is_blocked(name: str) -> bool:
    if not name:
        return False
    n = name.strip().lower()
    if n in _INFRA_BLOCKLIST_EXACT:
        return True
    return any(bad in n for bad in _INFRA_BLOCKLIST)


# Папки wiki/, которые lint НЕ считает за entity-страницы. Сюда попадают:
# - synthesis/ — это L3-синтез, а не entity;
# - concepts/ — legacy pre-типизации (до Этапа 3), оставлена для обратной
#   совместимости, новыми concept-страницами считаться не должна.
# NB: entities/ сюда НЕ входит — это активная папка, куда попадают Person/
# Topic/прочие типизированные entity (см. agent_manager bootstrap и
# promote-инструкции агентам про wiki/entities/).
_LINT_IGNORED_SUBDIRS = {"synthesis", "concepts"}

# Корневые md-файлы внутри wiki/, которые не являются entity-страницами.
_LINT_IGNORED_ROOT_FILES = {"index.md"}


def _collect_entity_pages(memory_path: Path) -> list[Path]:
    wiki = memory_path / "wiki"
    if not wiki.exists():
        return []
    pages: list[Path] = []
    for md in wiki.rglob("*.md"):
        # Скрытые файлы (lint-отчёт и т.п.)
        if md.name.startswith("."):
            continue
        rel = md.relative_to(memory_path)
        # wiki/index.md и другие корневые служебные страницы
        if len(rel.parts) == 2 and rel.parts[1] in _LINT_IGNORED_ROOT_FILES:
            continue
        # wiki/<subdir>/... — пропускаем legacy и synthesis
        if len(rel.parts) >= 3 and rel.parts[1] in _LINT_IGNORED_SUBDIRS:
            continue
        pages.append(md)
    return pages


def lint_wiki(agent_dir: str) -> LintReport:
    """Прогнать все проверки и вернуть структурированный отчёт."""
    memory_path = memory.get_memory_path(agent_dir)
    report = LintReport()

    pages = _collect_entity_pages(memory_path)
    page_names_lower: dict[str, list[Path]] = defaultdict(list)
    for p in pages:
        page_names_lower[p.stem.lower()].append(p)

    graph_path = memory_path / "graph.json"
    graph: dict = {"edges": []}
    if graph_path.exists():
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    edges = graph.get("edges", []) or []
    active_edges = [e for e in edges if not e.get("superseded_by")]

    # 1. Блок-лист имён в entity-страницах
    for p in pages:
        if _name_is_blocked(p.stem):
            report.blocklist_hits.append(LintIssue(
                code="blocklist_entity",
                severity="error",
                message=f"Entity-страница имеет имя из блок-листа: {p.stem}",
                where=str(p.relative_to(memory_path)),
            ))

    # Тот же блок-лист для имён в графе
    seen_in_graph: set[str] = set()
    for e in active_edges:
        for endpoint_key in ("from", "to"):
            ep = e.get(endpoint_key, "")
            if ep and _name_is_blocked(ep) and ep.lower() not in seen_in_graph:
                seen_in_graph.add(ep.lower())
                report.blocklist_hits.append(LintIssue(
                    code="blocklist_edge",
                    severity="error",
                    message=f"Граф содержит ребро с инфраструктурным именем: {ep}",
                    where=f"edge {e.get('from')}→{e.get('to')} ({e.get('type')})",
                ))

    # 2. Orphan-страницы (нет упоминаний в активных edges)
    edge_names_lower: set[str] = set()
    for e in active_edges:
        if e.get("from"):
            edge_names_lower.add(e["from"].lower())
        if e.get("to"):
            edge_names_lower.add(e["to"].lower())

    for p in pages:
        if p.stem.lower() not in edge_names_lower:
            report.orphans.append(LintIssue(
                code="orphan_page",
                severity="warning",
                message=f"Entity-страница без рёбер в графе: {p.stem}",
                where=str(p.relative_to(memory_path)),
            ))

    # 3. Висячие edges (endpoint не имеет страницы)
    for e in active_edges:
        for endpoint_key in ("from", "to"):
            ep = e.get(endpoint_key, "")
            if ep and ep.lower() not in page_names_lower:
                report.dangling_edges.append(LintIssue(
                    code="dangling_edge",
                    severity="warning",
                    message=(
                        f"Edge ссылается на entity без страницы: {ep}"
                    ),
                    where=f"edge {e.get('from')}→{e.get('to')} ({e.get('type')})",
                ))

    # 4. Дубликаты — несколько страниц с одним именем (case-insensitive)
    for stem_lower, paths in page_names_lower.items():
        if len(paths) > 1:
            rels = ", ".join(str(p.relative_to(memory_path)) for p in paths)
            report.duplicates.append(LintIssue(
                code="duplicate_entity",
                severity="warning",
                message=f"Entity '{stem_lower}' имеет {len(paths)} страниц",
                where=rels,
            ))

    # 5. Противоречивые exclusive-edges без supersession
    by_from_type: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in active_edges:
        t = e.get("type", "")
        if t in _EXCLUSIVE_LINK_TYPES:
            by_from_type[(e.get("from", "").lower(), t)].append(e)

    for (from_l, t), bucket in by_from_type.items():
        if len(bucket) > 1:
            targets = sorted({e.get("to", "") for e in bucket})
            report.contradictions.append(LintIssue(
                code="exclusive_conflict",
                severity="error",
                message=(
                    f"Exclusive-связь {t} от {from_l!r} имеет {len(bucket)} "
                    f"активных целей: {', '.join(targets)}. Должна быть одна — "
                    f"остальные нужно пометить superseded_by."
                ),
                where=f"{from_l} -[{t}]-> {{{', '.join(targets)}}}",
            ))

    return report


def write_report(agent_dir: str, report: LintReport) -> Path:
    """Записать человеко-читаемый отчёт в wiki/.lint_report.md."""
    memory_path = memory.get_memory_path(agent_dir)
    wiki = memory_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    out = wiki / ".lint_report.md"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Wiki Lint Report — {now}",
        "",
        f"**Всего замечаний:** {report.total} (errors: {report.errors})",
        "",
    ]

    sections = [
        ("Блок-лист (инфраструктура в графе/wiki)", report.blocklist_hits),
        ("Orphan entity-страницы", report.orphans),
        ("Висячие edges", report.dangling_edges),
        ("Дубликаты сущностей", report.duplicates),
        ("Противоречия exclusive-связей", report.contradictions),
    ]
    for title, items in sections:
        lines.append(f"## {title} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("_OK, нечего исправлять._")
        else:
            for it in items:
                marker = "❌" if it.severity == "error" else "⚠️"
                lines.append(f"- {marker} **{it.code}**: {it.message}")
                if it.where:
                    lines.append(f"  - `{it.where}`")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def run_lint(agent_dir: str) -> LintReport:
    """Прогнать lint и сразу записать отчёт."""
    report = lint_wiki(agent_dir)
    try:
        write_report(agent_dir, report)
    except OSError as e:
        logger.warning(f"wiki_lint: не удалось записать отчёт: {e}")
    logger.info(
        f"wiki_lint: total={report.total}, errors={report.errors}"
    )
    return report
