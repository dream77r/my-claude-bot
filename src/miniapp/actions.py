"""Writable actions for the Mini App.

Two groups:
  * Agent lifecycle (stop/start/restart) — founder only.
  * Skill install/uninstall/refresh — founder can target any agent,
    a non-founder only their accessible agents.

Runtime primitives already exist (`FleetRuntime.start_agent` / `.stop_agent`,
`SkillPool.install_skill` / `.uninstall_skill`); these endpoints just expose
them through the same Telegram-initData auth as the rest of the Mini App.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from ..skill_pool import make_pool_from_env
from .auth import AuthenticatedUser, get_current_user

if TYPE_CHECKING:
    from ..agent import Agent
    from ..main import FleetRuntime

logger = logging.getLogger(__name__)


def _runtime_from(request: Request) -> "FleetRuntime":
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="runtime not attached to app.state",
        )
    return rt


def _require_founder(user: AuthenticatedUser) -> None:
    if not user.is_founder:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="founder only",
        )


def _resolve_target_agent(
    runtime: "FleetRuntime", user: AuthenticatedUser, name: str
) -> "Agent":
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


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/agents/{name}/stop")
    async def stop_agent(
        name: str,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        _require_founder(user)
        runtime = _runtime_from(request)
        ok, message = await runtime.stop_agent(name)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=message
            )
        logger.info("Mini App: stopped agent '%s' (by user %s)", name, user.user_id)
        return {"ok": True, "agent": name, "message": message}

    @router.post("/agents/{name}/start")
    async def start_agent(
        name: str,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        _require_founder(user)
        runtime = _runtime_from(request)
        ok, message = await runtime.start_agent(name)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=message
            )
        logger.info("Mini App: started agent '%s' (by user %s)", name, user.user_id)
        return {"ok": True, "agent": name, "message": message}

    @router.post("/agents/{name}/restart")
    async def restart_agent(
        name: str,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        _require_founder(user)
        runtime = _runtime_from(request)

        if name in runtime.running_agents():
            ok, message = await runtime.stop_agent(name)
            if not ok:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"stop failed: {message}",
                )

        ok, message = await runtime.start_agent(name)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"start failed: {message}",
            )
        logger.info("Mini App: restarted agent '%s' (by user %s)", name, user.user_id)
        return {"ok": True, "agent": name, "message": message}

    # ── Skill install / uninstall / refresh ──────────────────────────────

    @router.post("/agents/{name}/skills/install")
    async def install_skill(
        name: str,
        request: Request,
        payload: dict = Body(...),
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        runtime = _runtime_from(request)
        agent = _resolve_target_agent(runtime, user, name)

        skill = (payload.get("skill") or "").strip()
        if not skill:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="missing 'skill' in body",
            )
        overwrite = bool(payload.get("overwrite", False))

        pool = make_pool_from_env(Path(runtime.root))
        if pool is None or not pool.is_available():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="skill pool unavailable — refresh it first",
            )

        result = pool.install_skill(
            skill, Path(agent.agent_dir), overwrite=overwrite
        )
        if not result.ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=result.error
            )
        logger.info(
            "Mini App: installed skill '%s' into '%s' (by user %s)",
            skill, name, user.user_id,
        )
        return {
            "ok": True,
            "agent": name,
            "skill": skill,
            "installed_to": result.installed_to,
            "missing_memory": result.missing_memory,
            "has_scripts": result.has_scripts,
        }

    @router.post("/agents/{name}/skills/uninstall")
    async def uninstall_skill(
        name: str,
        request: Request,
        payload: dict = Body(...),
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        runtime = _runtime_from(request)
        agent = _resolve_target_agent(runtime, user, name)

        skill = (payload.get("skill") or "").strip()
        if not skill:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="missing 'skill' in body",
            )

        pool = make_pool_from_env(Path(runtime.root))
        if pool is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="skill pool disabled",
            )

        removed = pool.uninstall_skill(skill, Path(agent.agent_dir))
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"skill '{skill}' not installed for agent '{name}'",
            )
        logger.info(
            "Mini App: uninstalled skill '%s' from '%s' (by user %s)",
            skill, name, user.user_id,
        )
        return {"ok": True, "agent": name, "skill": skill}

    @router.post("/skills/pool/refresh")
    async def refresh_pool(
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        _require_founder(user)
        runtime = _runtime_from(request)
        pool = make_pool_from_env(Path(runtime.root))
        if pool is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="skill pool disabled",
            )
        try:
            pool.refresh()
        except Exception as e:
            logger.warning("pool refresh failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)
            ) from None
        return {"ok": True, "skills_count": len(pool.list_skills())}

    return router
