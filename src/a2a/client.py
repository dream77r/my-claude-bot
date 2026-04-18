"""A2A outbound client: вызов внешних A2A-агентов.

Минимальный httpx-обёртка над A2A JSON-RPC. Умеет:
  - получить Agent Card по URL (опциональная discovery-проверка)
  - отправить `message/send` с текстом и получить ответ

Сделано намеренно тонко: спецификация A2A v1.0 объёмная (Task
lifecycle, artifacts, streaming), но для 80% use-cases из нашего
продукта — «master дёргает внешнего агента с промптом и получает
ответ». Полный SDK-путь открыт: модели a2a.types парсят/сериализуют
всё, этот модуль — обёртка для удобства.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import httpx
from a2a.types import AgentCard, Message, Part, Role, TextPart

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0


class A2AClientError(Exception):
    """Базовая ошибка A2A-клиента."""


class A2ARemoteError(A2AClientError):
    """Удалённый агент вернул JSON-RPC error."""

    def __init__(self, code: int, message: str):
        super().__init__(f"A2A remote error {code}: {message}")
        self.code = code


@dataclass
class A2AResponse:
    """Результат message/send."""

    text: str
    raw_message: Message


async def fetch_agent_card(
    card_url: str,
    *,
    timeout: float = 10.0,
) -> AgentCard:
    """Получить и распарсить Agent Card по прямому URL."""
    async with httpx.AsyncClient(timeout=timeout) as http:
        r = await http.get(card_url)
    r.raise_for_status()
    return AgentCard.model_validate(r.json())


def _build_message(text: str, *, context_id: str | None = None) -> Message:
    return Message(
        role=Role.user,
        message_id=uuid.uuid4().hex,
        parts=[Part(root=TextPart(text=text))],
        context_id=context_id,
    )


def _extract_text(message: Message) -> str:
    chunks: list[str] = []
    for part in message.parts:
        root = part.root if hasattr(part, "root") else part
        if isinstance(root, TextPart):
            chunks.append(root.text)
    return "\n".join(chunks).strip()


async def call_agent(
    endpoint_url: str,
    prompt: str,
    *,
    auth_token: str | None = None,
    caller_name: str = "my-claude-bot",
    context_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> A2AResponse:
    """Отправить `message/send` на A2A-endpoint и вернуть ответ.

    Args:
        endpoint_url: полный URL endpoint'а агента (из card.url).
        prompt: текст запроса.
        auth_token: bearer для Authorization (если endpoint требует).
        caller_name: имя вызывающего (попадёт в X-A2A-Caller).
        context_id: ID контекста для продолжения беседы.
        timeout: общий таймаут в секундах.
    """
    request_id = uuid.uuid4().hex
    outgoing = _build_message(prompt, context_id=context_id)

    envelope = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": outgoing.model_dump(by_alias=True, exclude_none=True),
        },
    }

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-A2A-Caller": caller_name,
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    async with httpx.AsyncClient(timeout=timeout) as http:
        r = await http.post(endpoint_url, json=envelope, headers=headers)

    if r.status_code >= 400:
        raise A2AClientError(
            f"HTTP {r.status_code}: {r.text[:200]}"
        )

    try:
        body = r.json()
    except ValueError as e:
        raise A2AClientError(f"response is not JSON: {e}") from None

    if "error" in body:
        err = body["error"]
        raise A2ARemoteError(err.get("code", 0), err.get("message", ""))

    result = body.get("result")
    if not isinstance(result, dict):
        raise A2AClientError("missing or invalid 'result'")

    try:
        reply = Message.model_validate(result)
    except Exception as e:
        raise A2AClientError(f"result is not a valid Message: {e}") from None

    return A2AResponse(text=_extract_text(reply), raw_message=reply)
