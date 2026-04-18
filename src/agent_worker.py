"""
AgentWorker — связка Agent с MessageBus.

Читает сообщения из bus-очереди агента, вызывает Agent.call_claude(),
публикует ответ обратно в bus.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .bus import FleetBus, FleetMessage, MessageType
from .delegation import DELEGATION_TIMEOUT
from .file_handler import scan_outbox

if TYPE_CHECKING:
    from .agent import Agent

logger = logging.getLogger(__name__)


class AgentWorker:
    """
    Воркер агента: читает из bus → call_claude → публикует ответ.

    Одновременно обрабатывает по одному сообщению на chat_id
    (сериализация через _active набор).

    Mid-turn injection: если юзер пишет второе сообщение, пока агент
    ещё обрабатывает предыдущее, новое сообщение буферится в
    `_pending_followups[chat_id]`. По завершении текущего turn все
    буферизированные сообщения склеиваются в одно и публикуются в ту же
    bus-очередь — `run()` подхватит их как следующий turn. Это
    противоположность stream interruption: не теряем контекст ни
    текущего ответа, ни того, что юзер хотел добавить.
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
        # Follow-up сообщения, пришедшие пока turn ещё идёт.
        self._pending_followups: dict[int, list[FleetMessage]] = {}

    def cancel_task(self, chat_id: int) -> bool:
        """Отменить активную задачу для chat_id (для /stop)."""
        task = self._active_tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            self._active_tasks.pop(chat_id, None)
            # /stop должен ещё и выбросить накопленные follow-ups,
            # иначе после отмены они снова запустят новый turn.
            self._pending_followups.pop(chat_id, None)
            return True
        return False

    def is_busy(self) -> bool:
        """True если есть незавершённые задачи."""
        return any(not t.done() for t in self._active_tasks.values())

    def active_info(self) -> list[dict]:
        """Снимок активных задач для cockpit API."""
        out = []
        for chat_id, task in self._active_tasks.items():
            if task.done():
                continue
            out.append({"chat_id": chat_id, "name": task.get_name()})
        return out

    async def run(self) -> None:
        """Основной цикл: читать из bus, обрабатывать, отвечать."""
        self._running = True
        logger.info(f"AgentWorker '{self.agent.name}' запущен")

        while self._running:
            try:
                msg = await self.bus.consume(self._queue_name)
                # Mid-turn injection: если для этого chat_id уже идёт
                # turn, буферируем сообщение вместо старта нового. В
                # finally _handle_message coalesced follow-ups вернутся
                # в эту же очередь как единое «следующее» сообщение.
                # Delegation (AGENT_TO_AGENT) идёт мимо буфера —
                # у них chat_id=0 и они не конкурируют с юзером.
                if (
                    msg.chat_id
                    and msg.msg_type != MessageType.AGENT_TO_AGENT
                    and self._has_active_task(msg.chat_id)
                ):
                    self._pending_followups.setdefault(
                        msg.chat_id, []
                    ).append(msg)
                    logger.info(
                        f"Mid-turn: буферизую follow-up для chat={msg.chat_id} "
                        f"(очередь={len(self._pending_followups[msg.chat_id])})"
                    )
                    thread_id = msg.metadata.get("message_thread_id")
                    queued_meta = {"event": "queued_followup"}
                    if thread_id:
                        queued_meta["message_thread_id"] = thread_id
                    await self.bus.publish(FleetMessage(
                        source=f"agent:{self.agent.name}",
                        target=f"telegram:{self.agent.name}",
                        content="",
                        msg_type=MessageType.SYSTEM,
                        chat_id=msg.chat_id,
                        metadata=queued_meta,
                    ))
                    continue
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

    def _has_active_task(self, chat_id: int) -> bool:
        task = self._active_tasks.get(chat_id)
        return task is not None and not task.done()

    @staticmethod
    def _coalesce_followups(msgs: list[FleetMessage]) -> FleetMessage:
        """Склеить буферизированные follow-ups в одно сообщение.

        - content: `\\n\\n`-joined non-empty parts в исходном порядке.
        - files: объединённый список (ничего не теряем).
        - metadata: от последнего сообщения (свежие thread_id,
          group_chat_id, etc), плюс `coalesced_count`.
        """
        contents = [m.content for m in msgs if m.content]
        merged_content = "\n\n".join(contents)
        merged_files: list = []
        for m in msgs:
            if m.files:
                merged_files.extend(m.files)
        last = msgs[-1]
        metadata = dict(last.metadata) if last.metadata else {}
        metadata["coalesced_count"] = len(msgs)
        return FleetMessage(
            source=last.source,
            target=last.target,
            content=merged_content,
            msg_type=last.msg_type,
            chat_id=last.chat_id,
            files=merged_files or None,
            metadata=metadata,
        )

    async def _handle_message(self, msg: FleetMessage) -> None:
        """Обработать одно входящее сообщение."""
        # Делегированное сообщение от другого агента — обработать и ответить
        if msg.msg_type == MessageType.AGENT_TO_AGENT:
            await self._handle_delegation(msg)
            return

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

            # Проверить outbox на файлы для отправки
            outbox_files = scan_outbox(self.agent.agent_dir)
            if outbox_files:
                logger.info(
                    f"Outbox: {len(outbox_files)} файл(ов) для отправки "
                    f"от '{self.agent.name}'"
                )

            # Опубликовать ответ (с файлами из outbox если есть)
            # Очистка outbox происходит в telegram_bridge ПОСЛЕ отправки файлов
            await self.bus.publish(FleetMessage(
                source=f"agent:{self.agent.name}",
                target=f"telegram:{self.agent.name}",
                content=response,
                msg_type=MessageType.OUTBOUND,
                chat_id=chat_id,
                files=outbox_files,
                metadata={
                    **base_meta,
                    "event": "response",
                    "in_reply_to": msg.id,
                    "agent_dir": self.agent.agent_dir if outbox_files else "",
                },
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
            # Identity check: pop только если в словаре всё ещё наша задача.
            # Иначе мы могли бы удалить только что созданный task-преемник.
            current = asyncio.current_task()
            if chat_id and self._active_tasks.get(chat_id) is current:
                self._active_tasks.pop(chat_id, None)

            # Mid-turn injection: если во время turn накопились follow-ups,
            # склеиваем и публикуем их как следующее сообщение для run().
            # ВАЖНО: pop из _active_tasks уже произошёл выше, так что
            # `run()` возьмёт merged сообщение в новую задачу, а не
            # положит снова в буфер.
            if chat_id:
                followups = self._pending_followups.pop(chat_id, None)
                if followups:
                    merged = self._coalesce_followups(followups)
                    logger.info(
                        f"Mid-turn: публикую coalesced follow-up для "
                        f"chat={chat_id} ({len(followups)} сообщений, "
                        f"{len(merged.content)} символов)"
                    )
                    try:
                        await self.bus.publish(merged)
                    except Exception as e:
                        logger.error(
                            f"Mid-turn publish failed chat={chat_id}: {e}"
                        )

    async def _handle_delegation(self, msg: FleetMessage) -> None:
        """Обработать делегированное сообщение от другого агента."""
        reply_to = msg.metadata.get("reply_to")
        source_agent = msg.metadata.get("source_agent", msg.source)
        source_role = msg.metadata.get("source_role", "")

        # Worker принимает задачи только от master
        if source_role != "master":
            logger.warning(
                f"Делегация отклонена: '{source_agent}' (role={source_role}) "
                f"→ '{self.agent.name}'. Только master может делегировать."
            )
            if reply_to:
                await self.bus.publish(FleetMessage(
                    source=f"agent:{self.agent.name}",
                    target=reply_to,
                    content=(
                        f"⚠️ Делегация отклонена. Агент '{source_agent}' "
                        f"не имеет прав делегировать задачи. "
                        f"Только master-агент может давать задания."
                    ),
                    msg_type=MessageType.AGENT_TO_AGENT,
                    metadata={
                        "delegation_id": msg.metadata.get("delegation_id", ""),
                    },
                ))
            return

        # Защита от рекурсивной делегации (A → B → A)
        delegation_chain = msg.metadata.get("delegation_chain", [])
        if self.agent.name in delegation_chain:
            logger.warning(
                f"Рекурсивная делегация заблокирована: "
                f"{' → '.join(delegation_chain)} → {self.agent.name}"
            )
            if reply_to:
                await self.bus.publish(FleetMessage(
                    source=f"agent:{self.agent.name}",
                    target=reply_to,
                    content=(
                        f"⚠️ Рекурсивная делегация заблокирована. "
                        f"Цепочка: {' → '.join(delegation_chain)} → "
                        f"{self.agent.name}. Реши задачу самостоятельно."
                    ),
                    msg_type=MessageType.AGENT_TO_AGENT,
                    metadata={
                        "delegation_id": msg.metadata.get("delegation_id", ""),
                    },
                ))
            return

        logger.info(
            f"AgentWorker '{self.agent.name}' получил делегацию "
            f"от '{source_agent}'"
        )

        # chat_id для уведомлений пользователю:
        # берём из метаданных или fallback на первого allowed_user агента
        notify_chat_id = (
            msg.metadata.get("notify_chat_id")
            or (self.agent.allowed_users[0] if self.agent.allowed_users else 0)
        )

        # Уведомить пользователя что задача принята
        if notify_chat_id:
            task_preview = msg.content
            # Убрать префикс "[Делегация от X]: " для читаемости
            if task_preview.startswith("[Делегация от "):
                task_preview = task_preview.split("]: ", 1)[-1]
            preview = task_preview[:300] + "..." if len(task_preview) > 300 else task_preview
            await self.bus.publish(FleetMessage(
                source=f"agent:{self.agent.name}",
                target=f"telegram:{self.agent.name}",
                content=f"📋 Взял задачу от {source_agent}:\n\n{preview}",
                msg_type=MessageType.OUTBOUND,
                chat_id=notify_chat_id,
                metadata={"event": "response"},
            ))

        try:
            async with self.semaphore:
                response = await asyncio.wait_for(
                    self.agent.call_claude(msg.content),
                    timeout=DELEGATION_TIMEOUT,
                )
        except asyncio.TimeoutError:
            response = f"⚠️ Таймаут: задача от {source_agent} не выполнена за {DELEGATION_TIMEOUT}с."
            logger.warning(
                f"Делегация таймаут: '{self.agent.name}' не успел ответить"
            )
        except Exception as e:
            response = f"❌ Ошибка выполнения задачи от {source_agent}: {e}"
            logger.error(f"Делегация ошибка в '{self.agent.name}': {e}")

        # Уведомить пользователя с результатом
        if notify_chat_id:
            await self.bus.publish(FleetMessage(
                source=f"agent:{self.agent.name}",
                target=f"telegram:{self.agent.name}",
                content=response,
                msg_type=MessageType.OUTBOUND,
                chat_id=notify_chat_id,
                metadata={"event": "response"},
            ))

        # Отправить ответ обратно через reply_to очередь
        if reply_to:
            await self.bus.publish(FleetMessage(
                source=f"agent:{self.agent.name}",
                target=reply_to,
                content=response,
                msg_type=MessageType.AGENT_TO_AGENT,
                metadata={
                    "delegation_id": msg.metadata.get("delegation_id", ""),
                    "source_agent": self.agent.name,
                },
            ))
            logger.info(
                f"Делегация ответ отправлен: '{self.agent.name}' → "
                f"'{source_agent}'"
            )

    def stop(self) -> None:
        """Остановить воркер."""
        self._running = False
