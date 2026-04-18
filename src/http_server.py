"""HTTP sidecar: общий FastAPI-сервер для Mini App и A2A.

Поднимается в том же event loop, что и Telegram-боты. По умолчанию слушает
127.0.0.1 (за reverse proxy). Читает env:

- HTTP_PORT: если пусто или 0 — сервер не запускается
- HTTP_HOST: по умолчанию 127.0.0.1

Шаг 0 (foundation): /health + /api/agents без аутентификации (только localhost).
Auth добавится в шаге 1 для Mini App, A2A — в шаге 7.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from pathlib import Path as _Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from .a2a.cards import build_router as _a2a_cards_router
from .a2a.server import build_router as _a2a_server_router
from .miniapp.actions import build_router as _miniapp_actions_router
from .miniapp.auth import AuthenticatedUser, get_current_user
from .miniapp.cockpit import build_router as _miniapp_cockpit_router
from .miniapp.routes import build_router as _miniapp_router

if TYPE_CHECKING:
    from .bus import FleetBus
    from .main import FleetRuntime

logger = logging.getLogger(__name__)


def create_app(
    runtime: "FleetRuntime",
    bus: "FleetBus | None" = None,
) -> FastAPI:
    app = FastAPI(
        title="my-claude-bot HTTP",
        docs_url="/docs" if os.environ.get("HTTP_DOCS") == "1" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if os.environ.get("HTTP_DOCS") == "1" else None,
    )
    app.state.runtime = runtime

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "agents_running": len(runtime.running_agents()),
        }

    @app.get("/api/agents")
    async def list_agents() -> dict:
        items = []
        for name in runtime.running_agents():
            agent = runtime.agents.get(name)
            if agent is None:
                continue
            items.append(
                {
                    "name": agent.name,
                    "display_name": agent.display_name,
                    "role": getattr(agent, "role", None),
                    "is_master": agent.is_master,
                }
            )
        return {"agents": items}

    @app.get("/api/me")
    async def whoami(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
        return {
            "user_id": user.user_id,
            "is_founder": user.is_founder,
            "origin_agent": user.origin_agent,
            "accessible_agents": user.accessible_agents,
        }

    app.include_router(_miniapp_router())
    app.include_router(_miniapp_cockpit_router())
    app.include_router(_miniapp_actions_router())
    app.include_router(_a2a_cards_router())
    if bus is not None:
        app.include_router(_a2a_server_router(bus))

    # Статика Mini App — `/miniapp/index.html` + /miniapp/assets/*.
    # Ищем в двух местах: рядом с репо (dev) или в env MINIAPP_DIR.
    miniapp_dir = _resolve_miniapp_dir()
    if miniapp_dir is not None:
        app.mount(
            "/miniapp",
            StaticFiles(directory=str(miniapp_dir), html=True),
            name="miniapp",
        )
        logger.info("Mini App static serve: %s", miniapp_dir)

    return app


def _resolve_miniapp_dir() -> "_Path | None":
    env = os.environ.get("MINIAPP_DIR", "").strip()
    if env:
        p = _Path(env)
        if p.is_dir() and (p / "index.html").exists():
            return p
        logger.warning("MINIAPP_DIR=%s invalid (нет index.html)", env)
        return None
    # Автопоиск: parent of src/ → miniapp/
    candidate = _Path(__file__).resolve().parent.parent / "miniapp"
    if candidate.is_dir() and (candidate / "index.html").exists():
        return candidate
    return None


async def serve_forever(runtime: "FleetRuntime") -> None:
    """Запустить uvicorn в текущем event loop. Возвращает при отмене.

    Если HTTP_PORT не задан или равен 0 — корутина завершается сразу.
    """
    port_raw = os.environ.get("HTTP_PORT", "").strip()
    try:
        port = int(port_raw) if port_raw else 0
    except ValueError:
        logger.warning("HTTP_PORT=%r не парсится, сервер не запущен", port_raw)
        return
    if port <= 0:
        logger.info("HTTP_PORT не задан — HTTP-сайдкар отключён")
        return

    host = os.environ.get("HTTP_HOST", "127.0.0.1").strip() or "127.0.0.1"

    import uvicorn

    app = create_app(runtime, bus=runtime.bus)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=os.environ.get("HTTP_LOG_LEVEL", "info"),
        access_log=os.environ.get("HTTP_ACCESS_LOG", "0") == "1",
        loop="none",
    )
    server = uvicorn.Server(config)
    logger.info("HTTP sidecar: listening on %s:%d", host, port)
    await server.serve()
