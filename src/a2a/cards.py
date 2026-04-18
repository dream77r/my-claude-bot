"""A2A Agent Cards — публичные карточки для обнаружения агентов.

Спецификация A2A v1.0 (Linux Foundation, 2026):
Agent Card — JSON-описание агента по well-known пути. Используется
другими агентами для discovery. Подписи карточек (Signed Agent Cards)
отложены на фазу 2 (требуют домен + ECDSA).

Endpoints публичные — карточка специально проектировалась как «визитка».
Используем pydantic-модели из a2a-sdk, чтобы гарантировать соответствие
спеке и чтобы внешние A2A-клиенты могли парсить ответ через
`A2ACardResolver`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
)
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ..skill_pool import extract_skill_metadata

if TYPE_CHECKING:
    from ..agent import Agent
    from ..main import FleetRuntime

logger = logging.getLogger(__name__)

A2A_PROTOCOL_VERSION = "0.3.0"
CARD_VERSION = "1.0.0"
PROVIDER_ORG = "my-claude-bot"


def _public_base_url(request: Request) -> str:
    env_url = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _agent_skills(agent: "Agent") -> list[AgentSkill]:
    skills_dir = Path(agent.agent_dir) / "skills"
    out: list[AgentSkill] = []
    if not skills_dir.exists():
        return out
    seen: set[str] = set()
    for skill_file in sorted(skills_dir.glob("*.md")):
        meta = extract_skill_metadata(skill_file) or {}
        sid = skill_file.stem
        if sid in seen:
            continue
        seen.add(sid)
        out.append(
            AgentSkill(
                id=sid,
                name=sid,
                description=meta.get("description", ""),
                tags=list(meta.get("tags") or []),
            )
        )
    for sub in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = sub / "SKILL.md"
        if not skill_md.exists():
            continue
        if sub.name in seen:
            continue
        seen.add(sub.name)
        meta = extract_skill_metadata(skill_md) or {}
        out.append(
            AgentSkill(
                id=sub.name,
                name=sub.name,
                description=meta.get("description", ""),
                tags=list(meta.get("tags") or []),
            )
        )
    return out


def build_agent_card(agent: "Agent", base_url: str) -> AgentCard:
    """Собрать AgentCard модель для сериализации по спеке A2A v1.0."""
    base = base_url.rstrip("/")
    description = (
        agent.config.get("description")
        or agent.config.get("display_name")
        or agent.name
    )
    return AgentCard(
        protocol_version=A2A_PROTOCOL_VERSION,
        name=agent.name,
        description=description,
        url=f"{base}/a2a/{agent.name}",
        version=CARD_VERSION,
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            state_transition_history=False,
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        provider=AgentProvider(
            organization=PROVIDER_ORG,
            url=base,
        ),
        skills=_agent_skills(agent),
    )


def _card_json_response(card: AgentCard) -> JSONResponse:
    """Сериализовать с by_alias → camelCase по спеке, убрать None-поля."""
    payload = card.model_dump(by_alias=True, exclude_none=True)
    return JSONResponse(payload)


def _runtime_from(request: Request) -> "FleetRuntime":
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="runtime not attached to app.state",
        )
    return rt


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/.well-known/agent-card/{name}")
    async def agent_card(name: str, request: Request) -> JSONResponse:
        runtime = _runtime_from(request)
        agent = runtime.agents.get(name)
        if agent is None or name not in runtime.running_agents():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"agent '{name}' not found",
            )
        return _card_json_response(
            build_agent_card(agent, _public_base_url(request))
        )

    @router.get("/.well-known/agent-cards")
    async def agent_cards_index(request: Request) -> dict:
        runtime = _runtime_from(request)
        base = _public_base_url(request)
        return {
            "cards": [
                {
                    "name": name,
                    "url": f"{base}/.well-known/agent-card/{name}",
                }
                for name in runtime.running_agents()
            ],
        }

    return router
