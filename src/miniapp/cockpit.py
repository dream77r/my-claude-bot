"""Cockpit API — live обзор флота для Mini App дэшборда.

Endpoints:
  GET /api/agents/{name}/status     — busy/idle + активные задачи
  GET /api/activity?limit=20&agent= — агрегированный фид событий
  GET /api/stats?period=today       — метрики из metrics.py

Все требуют `get_current_user` и фильтруют агентов по `accessible_agents`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..memory import get_memory_path
from ..metrics import get_stats
from .auth import AuthenticatedUser, get_current_user

if TYPE_CHECKING:
    from ..agent import Agent
    from ..main import FleetRuntime

logger = logging.getLogger(__name__)

# Строка лога: "- [2026-04-18 14:05] user: hello world"
_LOG_LINE_RE = re.compile(
    r"^-\s*\[(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\]\s+"
    r"(?P<role>\w+):\s*(?P<text>.*)$"
)

# Безопасный лимит на чтение log.md (не тащим всё в память).
MAX_LOG_BYTES = 256 * 1024  # 256 KiB


def _runtime_from(request: Request) -> "FleetRuntime":
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="runtime not attached to app.state",
        )
    return rt


def _agents_for(
    runtime: "FleetRuntime", user: AuthenticatedUser, name_filter: str | None
) -> list["Agent"]:
    """Список агентов с учётом ACL и опциональной фильтрации по имени."""
    if name_filter:
        if not user.is_founder and name_filter not in user.accessible_agents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"no access to agent '{name_filter}'",
            )
        agent = runtime.agents.get(name_filter)
        if agent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"agent '{name_filter}' not found",
            )
        return [agent]
    names = (
        runtime.running_agents() if user.is_founder else user.accessible_agents
    )
    return [runtime.agents[n] for n in names if n in runtime.agents]


def _read_log_tail(path: Path, max_bytes: int = MAX_LOG_BYTES) -> str:
    """Прочитать хвост файла (последние max_bytes)."""
    if not path.exists():
        return ""
    size = path.stat().st_size
    if size <= max_bytes:
        return path.read_text(encoding="utf-8", errors="replace")
    with open(path, "rb") as fh:
        fh.seek(-max_bytes, 2)
        raw = fh.read()
    # Отрезать возможный обрезанный первый байт UTF-8
    return raw.decode("utf-8", errors="replace")


def _parse_log_entries(text: str, agent_name: str) -> list[dict]:
    """Разобрать log.md в список событий."""
    out: list[dict] = []
    for line in text.splitlines():
        m = _LOG_LINE_RE.match(line.strip())
        if not m:
            continue
        # Нормализуем timestamp в ISO для легкой сортировки на фронте.
        ts_raw = m.group("ts").replace(" ", "T")
        preview = m.group("text").strip()
        if len(preview) > 200:
            preview = preview[:200] + "…"
        out.append(
            {
                "ts": ts_raw,
                "agent": agent_name,
                "role": m.group("role"),
                "preview": preview,
            }
        )
    return out


def _aggregate_stats(per_agent: dict[str, dict]) -> dict:
    """Собрать агрегированные метрики из per-agent dict-ов."""
    total_calls = sum(s.get("total_calls", 0) for s in per_agent.values())
    total_errors = sum(s.get("errors", 0) for s in per_agent.values())
    total_tool_calls = sum(
        s.get("tool_calls", 0) for s in per_agent.values()
    )
    total_prompt = sum(
        s.get("total_prompt_chars", 0) for s in per_agent.values()
    )
    total_response = sum(
        s.get("total_response_chars", 0) for s in per_agent.values()
    )
    # avg_latency усредняем по total_calls агентов (weighted)
    weighted_sum = sum(
        s.get("avg_latency", 0) * s.get("total_calls", 0)
        for s in per_agent.values()
    )
    avg = round(weighted_sum / total_calls, 2) if total_calls else 0
    return {
        "total_calls": total_calls,
        "errors": total_errors,
        "tool_calls": total_tool_calls,
        "total_prompt_chars": total_prompt,
        "total_response_chars": total_response,
        "avg_latency": avg,
    }


# ── Router ─────────────────────────────────────────────────────────────────


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/agents/{name}/status")
    async def agent_status(
        name: str,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        runtime = _runtime_from(request)
        agents = _agents_for(runtime, user, name)
        agent = agents[0]
        worker = runtime.workers.get(name) if hasattr(runtime, "workers") else None

        if worker is None:
            return {
                "agent": name,
                "running": name in runtime.running_agents(),
                "busy": False,
                "active_count": 0,
                "active": [],
            }
        return {
            "agent": name,
            "running": name in runtime.running_agents(),
            "busy": worker.is_busy(),
            "active_count": sum(
                1 for t in worker._active_tasks.values() if not t.done()
            ),
            "active": worker.active_info(),
            "display_name": agent.display_name,
            "role": getattr(agent, "role", None),
        }

    @router.get("/activity")
    async def activity_feed(
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
        limit: int = Query(20, ge=1, le=200),
        agent: str | None = Query(None),
    ) -> dict:
        runtime = _runtime_from(request)
        agents = _agents_for(runtime, user, agent)

        events: list[dict] = []
        for a in agents:
            log_path = get_memory_path(a.agent_dir) / "log.md"
            text = _read_log_tail(log_path)
            if not text:
                continue
            events.extend(_parse_log_entries(text, a.name))

        events.sort(key=lambda e: e["ts"], reverse=True)
        return {
            "limit": limit,
            "agent": agent,
            "events": events[:limit],
        }

    @router.get("/stats")
    async def fleet_stats(
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
        period: str = Query("today"),
    ) -> dict:
        runtime = _runtime_from(request)
        agents = _agents_for(runtime, user, None)

        days = {"today": 1, "week": 7, "month": 30}.get(period)
        if days is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="period must be today|week|month",
            )

        by_agent: dict[str, dict] = {}
        for a in agents:
            try:
                by_agent[a.name] = get_stats(a.agent_dir, days=days)
            except Exception as e:
                logger.warning("stats read failed for %s: %s", a.name, e)
                by_agent[a.name] = {"error": str(e), "total_calls": 0}

        return {
            "period": period,
            "days": days,
            "totals": _aggregate_stats(by_agent),
            "by_agent": by_agent,
        }

    return router
