"""
MessageBus — центральная шина сообщений My Claude Bot.

Развязывает каналы (Telegram) от агентов. Каждый подписчик получает
свою asyncio.Queue, публикация рассылает сообщение всем подходящим.

FleetMessage — единый формат сообщения в шине.
FleetBus — pub/sub маршрутизатор.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """Типы сообщений в шине."""
    INBOUND = "inbound"          # Входящее от пользователя
    OUTBOUND = "outbound"        # Ответ агента → канал
    AGENT_TO_AGENT = "a2a"       # Между агентами
    SYSTEM = "system"            # Системные (heartbeat, dream, etc.)


@dataclass
class FleetMessage:
    """Единый формат сообщения в шине."""
    source: str            # "telegram", "agent:me", "system"
    target: str            # "orchestrator", "agent:coder", "telegram:{chat_id}"
    content: str
    msg_type: MessageType = MessageType.INBOUND
    session_id: str = ""
    chat_id: int = 0
    user_id: int = 0
    files: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)


class FleetBus:
    """
    Pub/sub шина сообщений на asyncio.Queue.

    Подписчики регистрируются по имени (например "orchestrator", "agent:me").
    Сообщения доставляются по полю target:
    - Точный адрес: "agent:me" → очередь "agent:me"
    - Broadcast: "*" → все подписчики
    - Prefix: "agent:*" → все подписчики с префиксом "agent:"
    """

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._running = False

    def subscribe(self, name: str, maxsize: int = 100) -> asyncio.Queue:
        """
        Зарегистрировать подписчика.

        Args:
            name: уникальное имя ("orchestrator", "agent:me", "telegram")
            maxsize: размер очереди

        Returns:
            asyncio.Queue для чтения сообщений
        """
        if name in self._queues:
            return self._queues[name]
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues[name] = q
        logger.info(f"Bus: подписчик '{name}' зарегистрирован")
        return q

    def unsubscribe(self, name: str) -> None:
        """Удалить подписчика."""
        self._queues.pop(name, None)
        logger.info(f"Bus: подписчик '{name}' удалён")

    async def publish(self, msg: FleetMessage) -> int:
        """
        Опубликовать сообщение в шину.

        Маршрутизация по msg.target:
        - "*" → все подписчики
        - "agent:*" → все agent:*
        - "agent:me" → точно agent:me

        Returns:
            Количество получателей
        """
        delivered = 0
        target = msg.target

        for name, queue in self._queues.items():
            if self._matches(name, target):
                try:
                    queue.put_nowait(msg)
                    delivered += 1
                except asyncio.QueueFull:
                    logger.warning(
                        f"Bus: очередь '{name}' переполнена, "
                        f"сообщение от '{msg.source}' потеряно"
                    )

        if delivered == 0:
            logger.warning(
                f"Bus: нет получателей для target='{target}' "
                f"(source='{msg.source}')"
            )

        return delivered

    async def consume(self, name: str) -> FleetMessage:
        """
        Прочитать одно сообщение из очереди подписчика.

        Args:
            name: имя подписчика

        Returns:
            FleetMessage

        Raises:
            KeyError: если подписчик не зарегистрирован
        """
        queue = self._queues.get(name)
        if queue is None:
            raise KeyError(f"Подписчик '{name}' не зарегистрирован")
        return await queue.get()

    @property
    def subscribers(self) -> list[str]:
        """Список имён подписчиков."""
        return list(self._queues.keys())

    @staticmethod
    def _matches(subscriber: str, target: str) -> bool:
        """Проверить, подходит ли подписчик под target."""
        if target == "*":
            return True
        if target.endswith(":*"):
            prefix = target[:-1]  # "agent:*" → "agent:"
            return subscriber.startswith(prefix)
        return subscriber == target
