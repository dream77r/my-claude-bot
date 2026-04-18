"""Read-only API для Mini App.

Endpoints:
  GET /api/agents/{name}/memory/tree  — обход memory/ агента
  GET /api/agents/{name}/memory/file  — чтение файла (?path=…)
  GET /api/agents/{name}/skills       — локальные скиллы агента
  GET /api/skills/pool                — каталог из shared skill pool

Все требуют `get_current_user` и проверяют доступ к конкретному агенту.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..memory import get_memory_path
from ..skill_pool import extract_skill_metadata, make_pool_from_env
from .auth import AuthenticatedUser, get_current_user

if TYPE_CHECKING:
    from ..agent import Agent
    from ..main import FleetRuntime

logger = logging.getLogger(__name__)

# Поддиректории memory/, которые не отдаём наружу (внутренние/чувствительные).
EXCLUDED_MEMORY_PREFIXES: tuple[str, ...] = (
    ".git",
    "sessions",    # Claude CLI session IDs
    "outbox",      # внутренние очереди
    "dispatch",
    "delegation",
)

# Разрешённые расширения для чтения файлов памяти.
READABLE_SUFFIXES: frozenset[str] = frozenset(
    {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml"}
)

MAX_READ_BYTES = 1 * 1024 * 1024  # 1 MiB


# ── Helpers ────────────────────────────────────────────────────────────────


def _runtime_from(request: Request) -> "FleetRuntime":
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="runtime not attached to app.state",
        )
    return rt


def _resolve_agent(
    request: Request, user: AuthenticatedUser, name: str
) -> "Agent":
    runtime = _runtime_from(request)
    agent = runtime.agents.get(name)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"agent '{name}' not found",
        )
    if not user.is_founder and name not in user.accessible_agents:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"no access to agent '{name}'",
        )
    return agent


def _is_excluded(relative_parts: tuple[str, ...]) -> bool:
    if not relative_parts:
        return False
    return relative_parts[0] in EXCLUDED_MEMORY_PREFIXES


def _safe_memory_path(agent: "Agent", relative: str) -> Path:
    """Резолвит путь внутри memory/ агента с защитой от traversal.

    Запрещает: абсолютные пути, `..`, симлинки, выход за пределы memory/.
    """
    if not relative or relative.strip() in ("/", "."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="path required"
        )
    if relative.startswith("/") or Path(relative).is_absolute():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="illegal path"
        )
    candidate = Path(relative)
    if ".." in candidate.parts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="illegal path"
        )

    root = get_memory_path(agent.agent_dir).resolve()
    full = (root / candidate).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="path escapes memory root",
        ) from None
    if full.is_symlink():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="symlinks disallowed"
        )
    if _is_excluded(candidate.parts):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="path excluded"
        )
    return full


def _walk_tree(root: Path) -> list[dict]:
    """Собрать плоский список узлов относительно root, отсортированный."""
    out: list[dict] = []
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            continue
        rel = entry.relative_to(root)
        if _is_excluded(rel.parts):
            continue
        if entry.is_dir():
            node = {
                "path": str(rel),
                "type": "dir",
                "size": None,
            }
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            node = {
                "path": str(rel),
                "type": "file",
                "size": size,
            }
        out.append(node)
    return out


# ── Router ─────────────────────────────────────────────────────────────────


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/agents/{name}/memory/tree")
    async def memory_tree(
        name: str,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        agent = _resolve_agent(request, user, name)
        root = get_memory_path(agent.agent_dir)
        if not root.exists():
            return {"agent": name, "root": str(root), "nodes": []}
        return {
            "agent": name,
            "root": str(root),
            "nodes": _walk_tree(root),
        }

    @router.get("/agents/{name}/memory/file")
    async def memory_file(
        name: str,
        request: Request,
        path: str = Query(..., min_length=1),
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        agent = _resolve_agent(request, user, name)
        full = _safe_memory_path(agent, path)
        if not full.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
            )
        if full.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="path is a directory"
            )
        if full.suffix.lower() not in READABLE_SUFFIXES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"extension '{full.suffix}' not readable",
            )
        size = full.stat().st_size
        if size > MAX_READ_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"file too large ({size} bytes)",
            )
        try:
            content = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="file not utf-8",
            ) from None
        return {
            "agent": name,
            "path": path,
            "size": size,
            "content": content,
        }

    @router.get("/agents/{name}/skills")
    async def agent_skills(
        name: str,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        agent = _resolve_agent(request, user, name)
        skills_dir = Path(agent.agent_dir) / "skills"
        items: list[dict] = []
        if skills_dir.exists():
            for skill_file in sorted(skills_dir.glob("*.md")):
                meta = extract_skill_metadata(skill_file) or {}
                items.append(
                    {
                        "name": skill_file.stem,
                        "file": skill_file.name,
                        "title": meta.get("title", skill_file.stem),
                        "description": meta.get("description", ""),
                        "version": meta.get("version", ""),
                        "tags": meta.get("tags", []),
                    }
                )
            # bundle-скиллы (директории с SKILL.md)
            for sub in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
                skill_md = sub / "SKILL.md"
                if not skill_md.exists():
                    continue
                meta = extract_skill_metadata(skill_md) or {}
                items.append(
                    {
                        "name": sub.name,
                        "file": f"{sub.name}/SKILL.md",
                        "title": meta.get("title", sub.name),
                        "description": meta.get("description", ""),
                        "version": meta.get("version", ""),
                        "tags": meta.get("tags", []),
                        "bundle": True,
                    }
                )
        return {"agent": name, "skills": items}

    @router.get("/skills/pool")
    async def skills_pool(
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        runtime = _runtime_from(request)
        pool = make_pool_from_env(Path(runtime.root))
        if pool is None:
            return {"available": False, "reason": "pool disabled", "skills": []}
        if not pool.is_available():
            return {
                "available": False,
                "reason": "pool cache missing — run refresh first",
                "skills": [],
            }
        try:
            entries = pool.list_skills()
        except Exception as e:
            logger.warning("skill pool list failed: %s", e)
            return {"available": False, "reason": str(e), "skills": []}
        return {
            "available": True,
            "skills": [asdict(e) for e in entries],
        }

    return router
