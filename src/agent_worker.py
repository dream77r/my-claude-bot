"""
AgentWorker — связка Agent с MessageBus.

Читает сообщения из bus-очереди агента, вызывает Agent.call_claude(),
публикует ответ обратно в bus.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .bus import FleetBus, FleetMessage, MessageType

if TYPE_CHECKING:
    from .agent import Agent

logger = logging.getLogger(__name__)


class AgentWorker:
    """
    Воркер агента: читает из bus → call_claude → публикует ответ.

    Одновременно обрабатывает по одному сообщению на chat_id
    (сериализация через _active набор).
    """

    def __init__(
        self,
        agent: "Agent",
        bus: FleetBus,
        semaphore: asyncio.Semaphore,
    ):
        self.agent = agent
        self.bus = bus
        self.semaphore = semaphore
        self._queue_name = f"agent:{agent.name}"
        self._running = False
        # Активные chat_id → Task (для /stop)
        self._active_tasks: dict[int, asyncio.Task] = {}

    def cancel_task(self, chat_id: int) -> bool:
        """Отменить активную задачу для chat_id (для /stop)."""
        task = self._active_tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            self._active_tasks.pop(chat_id, None)
            return True
        return False

    async def run(self) -> None:
        """Основной цикл: читать из bus, обрабатывать, отвечать."""
        self._running = True
        logger.info(f"AgentWorker '{self.agent.name}' запущен")

        while self._running:
            try:
                msg = await self.bus.consume(self._queue_name)
                # Обработать в отдельной задаче (не блокируем очередь)
                task = asyncio.create_task(self._handle_message(msg))
                if msg.chat_id:
                    self._active_tasks[msg.chat_id] = task
            except asyncio.CancelledError:
                logger.info(f"AgentWorker '{self.agent.name}' остановлен")
                break
            except Exception as e:
                logger.error(f"AgentWorker '{self.agent.name}' error: {e}")

        self._running = False

    async def _handle_message(self, msg: FleetMessage) -> None:
        """Обработать одно входящее сообщение."""
        chat_id = msg.chat_id
        # Пробросить thread_id через все ответные сообщения
        thread_id = msg.metadata.get("message_thread_id")
        base_meta = {"message_thread_id": thread_id} if thread_id else {}

        # Уведомить bridge что начали обработку
        await self.bus.publish(FleetMessage(
            source=f"agent:{self.agent.name}",
            target=f"telegram:{self.agent.name}",
            content="",
            msg_type=MessageType.SYSTEM,
            chat_id=chat_id,
            metadata={**base_meta, "event": "processing_started"},
        ))

        try:
            # Колбек для tool hints — пересылаем через bus
            async def on_tool_use(hint: str):
                await self.bus.publish(FleetMessage(
                    source=f"agent:{self.agent.name}",
                    target=f"telegram:{self.agent.name}",
                    content=hint,
                    msg_type=MessageType.SYSTEM,
                    chat_id=chat_id,
                    metadata={**base_meta, "event": "tool_use"},
                ))

            # Колбек для streaming текста
            async def on_text_delta(accumulated_text: str):
                await self.bus.publish(FleetMessage(
                    source=f"agent:{self.agent.name}",
                    target=f"telegram:{self.agent.name}",
                    content=accumulated_text,
                    msg_type=MessageType.SYSTEM,
                    chat_id=chat_id,
                    metadata={**base_meta, "event": "text_delta"},
                ))

            group_chat_id = msg.metadata.get("group_chat_id")
            response = await self.agent.call_claude(
                msg.content,
                msg.files or None,
                self.semaphore,
                on_tool_use=on_tool_use,
                on_text_delta=on_text_delta,
                group_chat_id=group_chat_id,
            )

            # Опубликовать ответ
            await self.bus.publish(FleetMessage(
                source=f"agent:{self.agent.name}",
                target=f"telegram:{self.agent.name}",
                content=response,
                msg_type=MessageType.OUTBOUND,
                chat_id=chat_id,
                metadata={**base_meta, "event": "response", "in_reply_to": msg.id},
            ))

        except asyncio.CancelledError:
            logger.info(f"Task cancelled for chat {chat_id}")
        except asyncio.TimeoutError:
            await self.bus.publish(FleetMessage(
                source=f"agent:{self.agent.name}",
                target=f"telegram:{self.agent.name}",
                content="Ответ занял слишком долго. Попробуй переформулировать.",
                msg_type=MessageType.OUTBOUND,
                chat_id=chat_id,
                metadata={**base_meta, "event": "error", "error": "timeout"},
            ))
        except Exception as e:
            logger.error(f"AgentWorker handle error: {e}")
            await self.bus.publish(FleetMessage(
                source=f"agent:{self.agent.name}",
                target=f"telegram:{self.agent.name}",
                content="Произошла ошибка. Попробуй ещё раз.",
                msg_type=MessageType.OUTBOUND,
                chat_id=chat_id,
                metadata={**base_meta, "event": "error", "error": str(e)},
            ))
        finally:
            self._active_tasks.pop(chat_id, None)

    def stop(self) -> None:
        """Остановить воркер."""
        self._running = False
