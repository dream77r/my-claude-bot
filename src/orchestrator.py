"""
Orchestrator — маршрутизация сообщений между агентами.

Работает через MessageBus:
- Читает сообщения с target="orchestrator"
- Определяет нужного агента по chat_id, prefix-команде или auto-routing
- Пересылает сообщение агенту
- При одном агенте в fleet — прозрачный passthrough

Каждый агент = отдельный Telegram бот с bot_token.
Агенты НЕ шарят memory/.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .bus import FleetBus, FleetMessage, MessageType

if TYPE_CHECKING:
    from .agent import Agent

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Маршрутизатор сообщений между агентами.

    При одном агенте — прямой passthrough (без оверхеда).
    При нескольких — маршрутизация по:
    1. Прямое обращение: target="agent:{name}" (из prefix-команды /team_{name})
    2. Привязка chat_id к агенту (каждый бот = свой chat)
    3. Auto-routing (будущее расширение)
    """

    def __init__(self, bus: FleetBus, agents: dict[str, "Agent"]):
        """
        Args:
            bus: шина сообщений
            agents: словарь имя → Agent
        """
        self.bus = bus
        self.agents = agents
        self._queue = bus.subscribe("orchestrator")
        # Маппинг chat_id → agent_name (для multi-bot: каждый бот свой chat)
        self._chat_agent_map: dict[int, str] = {}
        self._running = False

    @property
    def is_single_agent(self) -> bool:
        """Один агент в fleet — упрощённая маршрутизация."""
        return len(self.agents) == 1

    def register_chat(self, chat_id: int, agent_name: str) -> None:
        """Привязать chat_id к агенту (вызывается из TelegramBridge)."""
        self._chat_agent_map[chat_id] = agent_name

    def resolve_agent(self, msg: FleetMessage) -> str | None:
        """
        Определить имя агента для сообщения.

        Приоритет:
        1. Явный target "agent:{name}" → name
        2. Привязка chat_id → agent_name
        3. Single agent → единственный агент
        """
        # 1. Явный target
        if msg.target.startswith("agent:"):
            name = msg.target.split(":", 1)[1]
            if name in self.agents:
                return name
            logger.warning(f"Orchestrator: агент '{name}' не найден")
            return None

        # 2. По chat_id
        if msg.chat_id and msg.chat_id in self._chat_agent_map:
            return self._chat_agent_map[msg.chat_id]

        # 3. Single agent
        if self.is_single_agent:
            return next(iter(self.agents))

        logger.warning(
            f"Orchestrator: не удалось определить агента для "
            f"msg от '{msg.source}' (chat_id={msg.chat_id})"
        )
        return None

    async def route_message(self, msg: FleetMessage) -> bool:
        """
        Маршрутизировать одно сообщение.

        Returns:
            True если сообщение доставлено
        """
        agent_name = self.resolve_agent(msg)
        if not agent_name:
            return False

        # Переадресовать сообщение агенту
        routed = FleetMessage(
            source=msg.source,
            target=f"agent:{agent_name}",
            content=msg.content,
            msg_type=msg.msg_type,
            session_id=msg.session_id,
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            files=msg.files,
            metadata={**msg.metadata, "routed_by": "orchestrator"},
            id=msg.id,
            timestamp=msg.timestamp,
        )

        delivered = await self.bus.publish(routed)
        return delivered > 0

    async def run(self) -> None:
        """
        Основной цикл оркестратора.

        Читает из своей очереди и маршрутизирует сообщения.
        """
        self._running = True
        logger.info(
            f"Orchestrator запущен, агентов: {len(self.agents)} "
            f"({', '.join(self.agents.keys())})"
        )

        while self._running:
            try:
                msg = await self.bus.consume("orchestrator")
                logger.debug(
                    f"Orchestrator: msg от '{msg.source}' → "
                    f"target='{msg.target}'"
                )

                ok = await self.route_message(msg)
                if not ok:
                    # Ответить отправителю что агент не найден
                    if msg.chat_id:
                        error_msg = FleetMessage(
                            source="orchestrator",
                            target=f"telegram:{msg.chat_id}",
                            content="Не удалось определить агента для этого запроса.",
                            msg_type=MessageType.OUTBOUND,
                            chat_id=msg.chat_id,
                        )
                        await self.bus.publish(error_msg)

            except asyncio.CancelledError:
                logger.info("Orchestrator остановлен")
                break
            except Exception as e:
                logger.error(f"Orchestrator error: {e}")

        self._running = False

    def stop(self) -> None:
        """Остановить оркестратор."""
        self._running = False
