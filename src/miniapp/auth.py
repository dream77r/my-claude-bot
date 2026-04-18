"""Telegram Mini App auth: валидация initData по HMAC-SHA256.

Алгоритм (спецификация Telegram WebApp):

  1. Разобрать initData как URL-encoded key=value пары.
  2. Извлечь поле `hash`, остальные отсортировать по ключу и склеить
     строками вида `key=value`, разделёнными `\n` — это data_check_string.
  3. secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token).digest()
  4. expected = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest()
  5. Сравнить `expected` с `hash` в константное время.
  6. Проверить `auth_date` — не старше max_age секунд.

Каждый агент флота имеет свой bot_token. Клиент обязан передать
`X-Origin-Agent: <name>` (или ?origin_agent=<name>`) — по этому имени
сервер выбирает bot_token для валидации. Авторизация (доступ к агентам)
считается по user_id против `agent.allowed_users` для всего флота.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Query, Request, status

if TYPE_CHECKING:
    from ..main import FleetRuntime

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Ошибка валидации initData."""


def parse_init_data(raw: str) -> dict[str, str]:
    """Разобрать initData в словарь. keep_blank_values на случай пустых полей."""
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=False)
    return dict(pairs)


def validate_init_data(
    raw: str,
    bot_token: str,
    max_age: int = 3600,
    now: float | None = None,
) -> dict[str, str]:
    """Проверить initData. Вернуть распарсенный dict или кинуть AuthError."""
    if not raw:
        raise AuthError("empty initData")
    if not bot_token:
        raise AuthError("empty bot_token")

    data = parse_init_data(raw)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise AuthError("hash missing")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(
        key=b"WebAppData", msg=bot_token.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    expected = hmac.new(
        key=secret_key, msg=check_string.encode("utf-8"), digestmod=hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("hash mismatch")

    auth_date_raw = data.get("auth_date")
    if not auth_date_raw:
        raise AuthError("auth_date missing")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as e:
        raise AuthError("auth_date not int") from e

    current = now if now is not None else time.time()
    if current - auth_date > max_age:
        raise AuthError(f"auth_date expired ({int(current - auth_date)}s old)")
    if auth_date - current > 60:
        raise AuthError("auth_date in the future")

    return data


def extract_user_id(init_fields: dict[str, str]) -> int:
    """Вытащить user.id из поля `user` (JSON-строка внутри initData)."""
    user_raw = init_fields.get("user")
    if not user_raw:
        raise AuthError("user field missing")
    try:
        user_obj = json.loads(user_raw)
    except json.JSONDecodeError as e:
        raise AuthError("user field not JSON") from e
    uid = user_obj.get("id")
    if not isinstance(uid, int):
        raise AuthError("user.id not int")
    return uid


def _founder_id() -> int:
    try:
        return int(os.environ.get("FOUNDER_TELEGRAM_ID", "0") or "0")
    except ValueError:
        return 0


@dataclass
class AuthenticatedUser:
    user_id: int
    is_founder: bool
    origin_agent: str
    accessible_agents: list[str]


def accessible_agents(runtime: "FleetRuntime", user_id: int) -> list[str]:
    """Агенты, к которым у пользователя есть доступ. Founder видит всё."""
    founder = _founder_id()
    if founder and user_id == founder:
        return list(runtime.running_agents())
    out = []
    for name in runtime.running_agents():
        agent = runtime.agents.get(name)
        if agent is None:
            continue
        if user_id in agent.allowed_users:
            out.append(name)
    return out


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_origin_agent: str | None = Header(default=None),
    origin_agent_q: str | None = Query(default=None, alias="origin_agent"),
) -> AuthenticatedUser:
    """FastAPI dependency: валидация initData + вычисление доступа.

    Заголовки:
      Authorization: tma <initData>
      X-Origin-Agent: <agent_name>   (или ?origin_agent=<name>)
    """
    if not authorization or not authorization.lower().startswith("tma "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing 'Authorization: tma <initData>' header",
        )
    init_data = authorization[4:].strip()

    origin = x_origin_agent or origin_agent_q
    if not origin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing X-Origin-Agent header or origin_agent query",
        )

    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="runtime not attached to app.state",
        )

    agent = runtime.agents.get(origin)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown origin agent '{origin}'",
        )

    try:
        fields = validate_init_data(init_data, agent.bot_token)
        user_id = extract_user_id(fields)
    except AuthError as e:
        logger.info("miniapp auth failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)
        ) from None

    founder = _founder_id()
    accessible = accessible_agents(runtime, user_id)
    is_founder = bool(founder) and user_id == founder
    if not is_founder and not accessible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user has no agent access",
        )

    return AuthenticatedUser(
        user_id=user_id,
        is_founder=is_founder,
        origin_agent=origin,
        accessible_agents=accessible,
    )
