"""A2A inbound server: JSON-RPC endpoint для внешних агентов.

Получает запросы по спеке A2A v1.0 `POST /a2a/{agent_name}` с телом
JSON-RPC 2.0. Поддерживает метод `message/send` (синхронная доставка).
Streaming (`message/stream` через SSE) и push-уведомления — фаза 2.

Адаптер: входящий A2A-запрос превращается в FleetMessage с
`msg_type=AGENT_TO_AGENT`, `source_role=master`, `reply_to=<corr_id>`.
Worker воркер обрабатывает как обычную делегацию и отвечает в канал
`a2a:resp:<corr_id>` (паттерн уже существующий в agent_worker.py).

Аутентификация: bearer `A2A_INBOUND_TOKEN`. Если токен не задан И
`A2A_INBOUND_ALLOW_UNAUTH != "1"` — все запросы отклоняются (safe
default, чтобы случайно выставленный наружу сервер не стал открытым
входом во флот).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from a2a.types import Message, Part, Role, TextPart
from fastapi import APIRouter, Header, HTTPException, Request, status

from ..bus import FleetMessage, MessageType

if TYPE_CHECKING:
    from ..bus import FleetBus
    from ..main import FleetRuntime

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0

# JSON-RPC 2.0 коды ошибок.
_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603


def _check_auth(authorization: str | None) -> None:
    token = os.environ.get("A2A_INBOUND_TOKEN", "").strip()
    allow_unauth = os.environ.get("A2A_INBOUND_ALLOW_UNAUTH", "").strip() == "1"
    if not token:
        if allow_unauth:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "A2A inbound disabled: set A2A_INBOUND_TOKEN or "
                "A2A_INBOUND_ALLOW_UNAUTH=1"
            ),
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing 'Authorization: Bearer <token>' header",
        )
    provided = authorization.split(None, 1)[1].strip()
    # constant-time compare
    import hmac as _hmac
    if not _hmac.compare_digest(provided, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bad token",
        )


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _extract_text(message: Message) -> str:
    """Склеить текст из всех TextPart'ов сообщения."""
    chunks: list[str] = []
    for part in message.parts:
        root = part.root if hasattr(part, "root") else part
        if isinstance(root, TextPart):
            chunks.append(root.text)
    return "\n".join(chunks).strip()


async def handle_message_send(
    runtime: "FleetRuntime",
    bus: "FleetBus",
    agent_name: str,
    params: dict,
    timeout: float = DEFAULT_TIMEOUT,
    source_caller: str = "external",
) -> Message:
    """Обработать `message/send`: публикация в bus + ожидание ответа.

    Возвращает Message для упаковки в JSON-RPC result.
    """
    if agent_name not in runtime.running_agents():
        raise ValueError(f"agent '{agent_name}' not running")

    message_raw = params.get("message")
    if not isinstance(message_raw, dict):
        raise ValueError("params.message required")

    try:
        incoming = Message.model_validate(message_raw)
    except Exception as e:
        raise ValueError(f"invalid Message: {e}") from None

    text = _extract_text(incoming)
    if not text:
        raise ValueError("message has no text parts")

    corr_id = uuid.uuid4().hex
    reply_channel = f"a2a:resp:{corr_id}"
    queue = bus.subscribe(reply_channel, maxsize=8)
    try:
        await bus.publish(
            FleetMessage(
                source=f"external:a2a:{source_caller}",
                target=f"agent:{agent_name}",
                content=text,
                msg_type=MessageType.AGENT_TO_AGENT,
                metadata={
                    "source_agent": source_caller,
                    "source_role": "master",
                    "reply_to": reply_channel,
                    "delegation_id": corr_id,
                    "a2a_context_id": incoming.context_id or "",
                },
            )
        )

        try:
            response_msg = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"agent '{agent_name}' did not reply within {timeout}s") from e

        reply_text = response_msg.content or ""
        return Message(
            role=Role.agent,
            message_id=uuid.uuid4().hex,
            parts=[Part(root=TextPart(text=reply_text))],
            context_id=incoming.context_id,
            reference_task_ids=None,
        )
    finally:
        bus.unsubscribe(reply_channel)


def build_router(bus: "FleetBus") -> APIRouter:
    """Построить A2A inbound router. Bus передаётся явно (а не через app.state),
    чтобы облегчить тестирование."""
    router = APIRouter()

    @router.post("/a2a/{name}")
    async def a2a_endpoint(
        name: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _check_auth(authorization)

        runtime = getattr(request.app.state, "runtime", None)
        if runtime is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="runtime not attached to app.state",
            )

        # Parse body
        try:
            raw_body = await request.body()
            envelope = json.loads(raw_body)
        except json.JSONDecodeError:
            return _jsonrpc_error(None, _JSONRPC_PARSE_ERROR, "invalid JSON")

        if not isinstance(envelope, dict):
            return _jsonrpc_error(None, _JSONRPC_INVALID_REQUEST, "not an object")

        req_id = envelope.get("id")
        method = envelope.get("method")
        params = envelope.get("params") or {}

        if envelope.get("jsonrpc") != "2.0":
            return _jsonrpc_error(
                req_id, _JSONRPC_INVALID_REQUEST, "jsonrpc must be '2.0'"
            )
        if not isinstance(method, str):
            return _jsonrpc_error(
                req_id, _JSONRPC_INVALID_REQUEST, "method required"
            )

        if method not in {"message/send"}:
            return _jsonrpc_error(
                req_id,
                _JSONRPC_METHOD_NOT_FOUND,
                f"method '{method}' not supported",
            )

        if not isinstance(params, dict):
            return _jsonrpc_error(
                req_id, _JSONRPC_INVALID_PARAMS, "params must be object"
            )

        timeout = float(params.get("timeout", DEFAULT_TIMEOUT))
        caller = request.headers.get("X-A2A-Caller", "external")

        try:
            reply = await handle_message_send(
                runtime, bus, name, params,
                timeout=timeout, source_caller=caller,
            )
        except ValueError as e:
            return _jsonrpc_error(req_id, _JSONRPC_INVALID_PARAMS, str(e))
        except TimeoutError as e:
            return _jsonrpc_error(req_id, _JSONRPC_INTERNAL_ERROR, str(e))
        except Exception as e:
            logger.exception("a2a inbound error")
            return _jsonrpc_error(req_id, _JSONRPC_INTERNAL_ERROR, str(e))

        return _jsonrpc_result(
            req_id,
            reply.model_dump(by_alias=True, exclude_none=True),
        )

    return router
