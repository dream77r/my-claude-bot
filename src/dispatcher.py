"""
Dispatcher — поллер исходящих сообщений из памяти агента в FleetBus.

Решает проблему маршрутизации сообщений, которые агент генерирует в
фоновых сценариях (cron, heartbeat, self-triggered skills), когда нет
активного запроса от пользователя и поэтому нет «текущего chat_id».

Агент в таких сценариях записывает JSON-файл в `memory/dispatch/` с явным
указанием chat_id и message_thread_id. Dispatcher сканирует эту папку,
публикует сообщения в bus как MessageType.OUTBOUND и удаляет
обработанные файлы. Повторные попытки происходят автоматически на
следующем цикле.

Формат файла `memory/dispatch/*.json`:
```json
{
  "chat_id": 123456789,
  "message_thread_id": 42,
  "text": "Текст сообщения",
  "parse_mode": "Markdown",
  "source": "morning_standup",
  "reminder_id": "optional-id"
}
```

Обязательные поля: `chat_id`, `text`.
Опциональные: `message_thread_id`, `parse_mode`, `source`, `reminder_id`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from .bus import FleetBus, FleetMessage, MessageType
from .fs_watcher import DirectoryWatcher

logger = logging.getLogger(__name__)

POLL_INTERVAL_DEFAULT = 5.0
MAX_DISPATCH_PER_CYCLE = 20


def _dispatch_dir(agent_dir: str) -> Path:
    return Path(agent_dir) / "memory" / "dispatch"


def _failed_dir(agent_dir: str) -> Path:
    return _dispatch_dir(agent_dir) / "failed"


def _load_and_validate(path: Path) -> dict | None:
    """Прочитать JSON, вернуть dict или None если невалиден."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Dispatcher: не могу прочитать {path.name}: {e}")
        return None

    if not isinstance(payload, dict):
        logger.warning(f"Dispatcher: {path.name} не dict")
        return None

    if "chat_id" not in payload or "text" not in payload:
        logger.warning(f"Dispatcher: {path.name} без chat_id/text")
        return None

    try:
        payload["chat_id"] = int(payload["chat_id"])
    except (TypeError, ValueError):
        logger.warning(f"Dispatcher: {path.name} chat_id не int")
        return None

    if payload.get("message_thread_id") is not None:
        try:
            payload["message_thread_id"] = int(payload["message_thread_id"])
        except (TypeError, ValueError):
            payload["message_thread_id"] = None

    if not isinstance(payload["text"], str) or not payload["text"].strip():
        logger.warning(f"Dispatcher: {path.name} пустой text")
        return None

    return payload


def _quarantine(path: Path, agent_dir: str) -> None:
    """Переместить сломанный файл в dispatch/failed/ для диагностики."""
    failed = _failed_dir(agent_dir)
    failed.mkdir(parents=True, exist_ok=True)
    dest = failed / f"{int(time.time())}_{path.name}"
    try:
        shutil.move(str(path), str(dest))
    except OSError as e:
        logger.error(f"Dispatcher: карантин {path.name} не удался: {e}")


async def _publish_one(
    path: Path,
    agent_name: str,
    bus: FleetBus,
) -> bool:
    """
    Обработать один dispatch-файл. Вернуть True при успехе.
    При неудаче валидации — поставить в карантин.
    При неудаче bus.publish — оставить файл для следующего цикла.
    """
    payload = _load_and_validate(path)
    if payload is None:
        _quarantine(path, str(path.parent.parent.parent))
        return False

    metadata = {
        "message_thread_id": payload.get("message_thread_id"),
        "source": payload.get("source", "dispatch"),
        "parse_mode": payload.get("parse_mode"),
    }
    if "reminder_id" in payload:
        metadata["reminder_id"] = payload["reminder_id"]

    msg = FleetMessage(
        source=f"agent:{agent_name}",
        target=f"telegram:{agent_name}",
        content=payload["text"],
        msg_type=MessageType.OUTBOUND,
        chat_id=payload["chat_id"],
        metadata=metadata,
    )

    try:
        delivered = await bus.publish(msg)
    except Exception as e:
        logger.error(f"Dispatcher: bus.publish упал для {path.name}: {e}")
        return False

    if delivered == 0:
        logger.warning(
            f"Dispatcher: нет получателей для {path.name} "
            f"(target=telegram:{agent_name}) — оставляю на следующий цикл"
        )
        return False

    try:
        path.unlink()
    except OSError as e:
        logger.error(f"Dispatcher: не могу удалить {path.name}: {e}")
        return False

    logger.info(
        f"Dispatcher: отправлено {path.name} → chat_id={payload['chat_id']} "
        f"thread={payload.get('message_thread_id')} source={metadata['source']}"
    )
    return True


async def dispatcher_loop(
    agent_dir: str,
    agent_name: str,
    bus: FleetBus,
    poll_interval: float = POLL_INTERVAL_DEFAULT,
) -> None:
    """
    Бесконечный цикл поллинга memory/dispatch/ агента.

    Args:
        agent_dir: путь к директории агента (содержит memory/)
        agent_name: имя агента для формирования target адреса
        bus: шина сообщений
        poll_interval: интервал поллинга в секундах
    """
    dispatch = _dispatch_dir(agent_dir)
    watcher = DirectoryWatcher(dispatch)
    watcher.start()

    logger.info(
        f"Dispatcher loop запущен для '{agent_name}': "
        f"{dispatch} (mode={watcher.mode}, safety_interval={poll_interval}s)"
    )

    try:
        while True:
            try:
                await watcher.wait(timeout=poll_interval)

                files = sorted(
                    p for p in dispatch.glob("*.json") if p.is_file()
                )[:MAX_DISPATCH_PER_CYCLE]

                for path in files:
                    await _publish_one(path, agent_name, bus)

            except asyncio.CancelledError:
                logger.info(f"Dispatcher loop '{agent_name}' остановлен")
                break
            except Exception as e:
                logger.error(f"Dispatcher loop error: {e}")
                await asyncio.sleep(poll_interval)
    finally:
        watcher.stop()
