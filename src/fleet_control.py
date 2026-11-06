"""Политика флота, которой рулит оркестратор (мастер).

Два независимых флага на агента:

- ``enabled`` — поднимать ли агента при старте платформы.
  ``false`` переживает перезапуск: ``load_agents`` его пропускает.
- ``notifications`` — слать ли проактивные сообщения (heartbeat, cron,
  дайджест, dispatch). Ответы на сообщения пользователя не глушатся.

Флаги пишутся в ``agents/{name}/agent.local.yaml`` между маркерами.
Остальной текст файла (комментарии, ``allowed_users``) не переписывается.
Доступ (``allowed_users``) хранится там же, отдельным блоком: пустой
список в оверлее заменяет базовый и открывает агента всем.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

FLEET_BEGIN = "# >>> fleet-control"
FLEET_END = "# <<< fleet-control"
ACCESS_BEGIN = "# >>> access-control"
ACCESS_END = "# <<< access-control"


@dataclass(frozen=True)
class FleetFlags:
    enabled: bool = True
    notifications: bool = True


def _as_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _read_mapping(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        expanded = os.path.expandvars(path.read_text(encoding="utf-8"))
        data = yaml.safe_load(expanded) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _fleet_dict(path: Path) -> dict:
    raw = _read_mapping(path).get("fleet") or {}
    return raw if isinstance(raw, dict) else {}


def load_flags(agent_dir: Path | str) -> FleetFlags:
    """Слить ``fleet`` из agent.yaml и agent.local.yaml. Оверлей побеждает."""
    agent_dir = Path(agent_dir)
    merged = {
        **_fleet_dict(agent_dir / "agent.yaml"),
        **_fleet_dict(agent_dir / "agent.local.yaml"),
    }
    return FleetFlags(
        enabled=_as_bool(merged.get("enabled"), True),
        notifications=_as_bool(merged.get("notifications"), True),
    )


def is_master_agent(agent_dir: Path | str) -> bool:
    role = str(_read_mapping(Path(agent_dir) / "agent.yaml").get("role") or "")
    return role == "master"


def _render_fleet(flags: FleetFlags) -> str:
    enabled = "true" if flags.enabled else "false"
    notifications = "true" if flags.notifications else "false"
    return (
        f"{FLEET_BEGIN}\n"
        "fleet:\n"
        f"  enabled: {enabled}\n"
        f"  notifications: {notifications}\n"
        f"{FLEET_END}\n"
    )


def _replace_block(text: str, begin: str, end: str, block: str) -> str:
    if begin in text and end in text:
        pre, rest = text.split(begin, 1)
        _, post = rest.split(end, 1)
        pre = pre.rstrip()
        post = post.lstrip("\n")
        glue = "\n\n" if pre else ""
        tail = f"\n{post}" if post else ""
        return f"{pre}{glue}{block}{tail}"
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    return text + block


def update_flags(
    agent_dir: Path | str,
    *,
    enabled: bool | None = None,
    notifications: bool | None = None,
) -> FleetFlags:
    """Обновить флаги. Непереданные поля остаются как были."""
    agent_dir = Path(agent_dir)
    current = load_flags(agent_dir)
    flags = FleetFlags(
        enabled=current.enabled if enabled is None else enabled,
        notifications=current.notifications if notifications is None else notifications,
    )
    path = agent_dir / "agent.local.yaml"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(
        _replace_block(existing, FLEET_BEGIN, FLEET_END, _render_fleet(flags)),
        encoding="utf-8",
    )
    return flags


def read_allowed_users(agent_dir: Path | str) -> list[int]:
    """Эффективный список доступа. Ключ в оверлее заменяет базовый целиком."""
    agent_dir = Path(agent_dir)
    local = _read_mapping(agent_dir / "agent.local.yaml")
    if "allowed_users" in local:
        raw = local.get("allowed_users") or []
    else:
        raw = _read_mapping(agent_dir / "agent.yaml").get("allowed_users") or []
    if not isinstance(raw, list):
        return []
    users: list[int] = []
    for item in raw:
        try:
            users.append(int(item))
        except (TypeError, ValueError):
            continue
    return users


def write_allowed_users(agent_dir: Path | str, users: list[int]) -> list[int]:
    """Записать доступ в оверлей, не трогая agent.yaml и чужие ключи."""
    agent_dir = Path(agent_dir)
    cleaned: list[int] = []
    for item in users:
        number = int(item)
        if number not in cleaned:
            cleaned.append(number)
    lines = [ACCESS_BEGIN, "allowed_users:"]
    if cleaned:
        lines.extend(f"  - {uid}" for uid in cleaned)
    else:
        lines.append("  []")
    lines.append(ACCESS_END)
    block = "\n".join(lines) + "\n"
    path = agent_dir / "agent.local.yaml"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(
        _replace_block(existing, ACCESS_BEGIN, ACCESS_END, block),
        encoding="utf-8",
    )
    return cleaned


def proactive_meta(origin: str, extra: dict | None = None) -> dict:
    """Метка проактивного сообщения: мост может его заглушить."""
    meta: dict = {"proactive": True, "origin_agent": origin}
    if extra:
        for key, value in extra.items():
            if value is not None:
                meta[key] = value
    return meta


def should_drop_proactive(msg: object, listener_agent_dir: Path | str) -> bool:
    """True, если это проактивное сообщение выключенного или заглушённого агента.

    Обычный ответ на реплику пользователя метки ``proactive`` не несёт
    и сюда не попадает.
    """
    meta = getattr(msg, "metadata", None) or {}
    if not meta.get("proactive"):
        return False
    origin = str(meta.get("origin_agent") or "")
    if not origin and str(getattr(msg, "source", "")).startswith("agent:"):
        origin = str(msg.source).split(":", 1)[1]
    if not origin or "/" in origin or "\\" in origin or origin.startswith("."):
        return False
    agent_dir = Path(listener_agent_dir).resolve().parent / origin
    if not (agent_dir / "agent.yaml").is_file():
        return False
    flags = load_flags(agent_dir)
    return (not flags.enabled) or (not flags.notifications)
