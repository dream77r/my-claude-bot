"""
DelegationManager — межагентная делегация задач.

Механизм:
1. Агент записывает delegation/{target}.task.md через Write tool
2. DelegationManager обнаруживает файл
3. Отправляет содержимое как AGENT_TO_AGENT сообщение через bus
4. Ждёт ответа (таймаут 120 сек)
5. Записывает ответ в delegation/{target}.result.md
6. Агент читает результат через Read tool
"""

import asyncio
import logging
import uuid
from pathlib import Path

from .bus import FleetBus, FleetMessage, MessageType

logger = logging.getLogger(__name__)

# Таймаут ожидания ответа от делегированного агента
DELEGATION_TIMEOUT = 600


class DelegationManager:
    """Мониторит файлы delegation/*.task.md для межагентной делегации."""

    def __init__(self, agent_name: str, agent_dir: str, bus: FleetBus):
        self.agent_name = agent_name
        self.agent_dir = agent_dir
        self.bus = bus
        self.delegation_dir = Path(agent_dir) / "memory" / "delegation"

    async def watch(self) -> None:
        """Бесконечный цикл проверки новых task-файлов."""
        self.delegation_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"DelegationManager '{self.agent_name}' запущен, "
            f"мониторит {self.delegation_dir}"
        )

        while True:
            try:
                await asyncio.sleep(1)
                for task_file in self.delegation_dir.glob("*.task.md"):
                    target_agent = task_file.name.replace(".task.md", "")
                    await self._process_delegation(target_agent, task_file)
            except asyncio.CancelledError:
                logger.info(f"DelegationManager '{self.agent_name}' остановлен")
                break
            except Exception as e:
                logger.error(f"DelegationManager error: {e}")

    async def _process_delegation(self, target: str, task_file: Path) -> None:
        """Обработать одну делегацию."""
        try:
            content = task_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Не удалось прочитать {task_file}: {e}")
            return

        # Удалить файл сразу, чтобы не обработать дважды
        task_file.unlink(missing_ok=True)

        logger.info(
            f"Делегация: {self.agent_name} → {target} "
            f"({len(content)} символов)"
        )

        # Уникальный ID для request-response
        request_id = uuid.uuid4().hex[:8]
        response_queue = f"delegation:{self.agent_name}:{request_id}"

        # Подписаться на очередь ответа
        self.bus.subscribe(response_queue)

        try:
            # Отправить задачу целевому агенту
            await self.bus.publish(FleetMessage(
                source=f"agent:{self.agent_name}",
                target=f"agent:{target}",
                content=f"[Делегация от {self.agent_name}]: {content}",
                msg_type=MessageType.AGENT_TO_AGENT,
                metadata={
                    "delegation_id": request_id,
                    "reply_to": response_queue,
                    "source_agent": self.agent_name,
                    "source_role": "master",
                    "delegation_chain": [self.agent_name],
                },
            ))

            # Ждать ответа с таймаутом
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume(response_queue),
                    timeout=DELEGATION_TIMEOUT,
                )
                result = msg.content
                logger.info(
                    f"Делегация ответ: {target} → {self.agent_name} "
                    f"({len(result)} символов)"
                )
            except asyncio.TimeoutError:
                result = (
                    f"⚠️ Агент '{target}' не ответил в течение "
                    f"{DELEGATION_TIMEOUT} секунд."
                )
                logger.warning(
                    f"Делегация таймаут: {target} не ответил за "
                    f"{DELEGATION_TIMEOUT}с"
                )
        finally:
            self.bus.unsubscribe(response_queue)

        # Записать результат
        result_file = self.delegation_dir / f"{target}.result.md"
        result_file.write_text(
            f"# Ответ от {target}\n\n{result}",
            encoding="utf-8",
        )
