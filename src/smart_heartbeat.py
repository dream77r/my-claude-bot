"""
Smart Heartbeat — умные триггеры для проактивного агента.

Расширяет базовый heartbeat: cron-триггеры с промптами,
утренний брифинг, вечерний дайджест, мониторинг дедлайнов.

Конфиг в agent.yaml:
```yaml
heartbeat:
  enabled: true
  interval_minutes: 30
  triggers:
    - name: "morning_briefing"
      schedule: "0 9 * * *"
      prompt: "Подготовь утренний брифинг..."
      model: "sonnet"
      notify: true
    - name: "deadline_check"
      schedule: "0 */4 * * *"
      prompt: "Проверь дедлайны..."
      model: "haiku"
      notify: "auto"
```
"""

import asyncio
import logging
from datetime import datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import get_claude_cli_path, memory
from .bus import FleetBus, FleetMessage, MessageType
from .cron import should_run
from .heartbeat import check_heartbeat

logger = logging.getLogger(__name__)


class SmartTrigger:
    """Один умный триггер."""

    def __init__(self, config: dict):
        self.name: str = config["name"]
        self.schedule: str = config["schedule"]  # cron format
        self.prompt: str = config["prompt"]
        self.model: str = config.get("model", "haiku")
        self.notify: bool | str = config.get("notify", "auto")  # true/false/"auto"
        self.allowed_tools: list[str] = config.get(
            "allowed_tools", ["Read", "Write", "Glob", "Grep"]
        )

    def should_run(self, now: datetime) -> bool:
        """Проверить cron-расписание. Использует логику из src/cron.py."""
        return should_run(self.schedule, now)


class SmartHeartbeat:
    """Умный heartbeat с триггерами."""

    def __init__(
        self,
        agent_dir: str,
        agent_name: str,
        config: dict,
        bus: FleetBus | None = None,
        chat_id: int = 0,
    ):
        self.agent_dir = agent_dir
        self.agent_name = agent_name
        self.bus = bus
        self.chat_id = chat_id
        self.triggers = [SmartTrigger(t) for t in config.get("triggers", [])]
        self.legacy_interval = config.get("interval_minutes", 30)
        self.legacy_enabled = config.get("enabled", True)
        # Защита от двойного запуска: последнее время запуска для каждого триггера
        self._last_run: dict[str, str] = {}
        # Время последнего legacy heartbeat
        self._last_legacy_run: datetime | None = None

    async def run(self) -> None:
        """Главный цикл: проверяет триггеры каждую минуту."""
        logger.info(
            f"SmartHeartbeat запущен для '{self.agent_name}': "
            f"{len(self.triggers)} триггеров "
            f"({', '.join(t.name for t in self.triggers)})"
        )

        while True:
            try:
                now = datetime.now()

                # Проверить триггеры
                for trigger in self.triggers:
                    minute_key = now.strftime("%Y-%m-%d %H:%M")
                    trigger_key = f"{trigger.name}:{minute_key}"

                    if trigger.should_run(now) and trigger_key not in self._last_run:
                        self._last_run[trigger_key] = minute_key
                        asyncio.create_task(self._execute_trigger(trigger))
                        # Очистить старые записи (оставить только за последний час)
                        self._cleanup_last_run(now)

                # Legacy heartbeat (HEARTBEAT.md) по интервалу
                if self.legacy_enabled:
                    await self._check_legacy_heartbeat(now)

                # Спать до следующей минуты
                sleep_seconds = 60 - datetime.now().second
                if sleep_seconds <= 0:
                    sleep_seconds = 60
                await asyncio.sleep(sleep_seconds)

            except asyncio.CancelledError:
                logger.info(f"SmartHeartbeat '{self.agent_name}' остановлен")
                break
            except Exception as e:
                logger.error(f"SmartHeartbeat loop error: {e}")
                await asyncio.sleep(60)

    async def _check_legacy_heartbeat(self, now: datetime) -> None:
        """Проверить HEARTBEAT.md по legacy-интервалу."""
        interval_seconds = self.legacy_interval * 60

        if self._last_legacy_run is None:
            self._last_legacy_run = now
            return

        elapsed = (now - self._last_legacy_run).total_seconds()
        if elapsed < interval_seconds:
            return

        self._last_legacy_run = now
        logger.debug(f"SmartHeartbeat: legacy heartbeat check для '{self.agent_name}'")

        try:
            result = await check_heartbeat(self.agent_dir)

            if result["should_notify"] and self.bus and self.chat_id:
                msg = FleetMessage(
                    source=f"agent:{self.agent_name}",
                    target=f"telegram:{self.agent_name}",
                    content=f"[Heartbeat] {result['notification']}",
                    msg_type=MessageType.OUTBOUND,
                    chat_id=self.chat_id,
                )
                await self.bus.publish(msg)
                logger.info(
                    f"Legacy heartbeat: уведомление отправлено в чат {self.chat_id}"
                )
        except Exception as e:
            logger.error(f"Legacy heartbeat error: {e}")

    async def _execute_trigger(self, trigger: SmartTrigger) -> None:
        """Выполнить триггер и уведомить если нужно."""
        logger.info(
            f"SmartTrigger '{trigger.name}' запущен для '{self.agent_name}'"
        )

        try:
            # 1. Вызвать Claude с промптом триггера
            response = await self._call_claude(
                trigger.prompt, trigger.model, trigger.allowed_tools
            )

            if not response:
                logger.warning(f"SmartTrigger '{trigger.name}': пустой ответ")
                return

            logger.info(
                f"SmartTrigger '{trigger.name}' завершён, "
                f"ответ: {len(response)} символов"
            )

            # 2. Записать в daily note
            self._log_to_daily(trigger.name, response)

            # 3. Git commit
            memory.git_commit(self.agent_dir, f"SmartTrigger: {trigger.name}")

            # 4. Решить об уведомлении
            should_notify = False
            if trigger.notify is True or trigger.notify == "true":
                should_notify = True
            elif trigger.notify is False or trigger.notify == "false":
                should_notify = False
            elif trigger.notify == "auto":
                should_notify = await self._evaluate_notification(response)

            # 5. Отправить уведомление
            if should_notify and self.bus:
                await self._send_notification(trigger.name, response)

        except Exception as e:
            logger.error(f"SmartTrigger '{trigger.name}' error: {e}")

    async def _call_claude(
        self,
        prompt: str,
        model: str = "haiku",
        allowed_tools: list[str] | None = None,
    ) -> str:
        """Вызов Claude. Повторяет паттерн из heartbeat.py _call_claude."""
        memory_path = memory.get_memory_path(self.agent_dir)

        options = ClaudeAgentOptions(
            model=model,
            permission_mode="bypassPermissions",
            cli_path=get_claude_cli_path(),
            cwd=str(memory_path),
        )
        if allowed_tools:
            options.allowed_tools = allowed_tools

        result_text = ""
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text
            elif isinstance(msg, ResultMessage):
                if msg.result and not result_text:
                    result_text = msg.result

        return result_text

    async def _evaluate_notification(self, response: str) -> bool:
        """Дешёвый LLM-вызов для решения, уведомлять ли пользователя."""
        eval_prompt = (
            f"Агент выполнил фоновую задачу. Вот результат:\n\n"
            f"{response[:1000]}\n\n"
            "Стоит ли уведомить пользователя об этом результате?\n"
            "Уведомляй только если:\n"
            "- Результат важный или срочный\n"
            "- Обнаружена проблема, требующая внимания\n"
            "- Есть конкретная информация для действий\n\n"
            "НЕ уведомляй если:\n"
            "- Ничего не найдено\n"
            "- Результат рутинный\n"
            "- Нет ничего нового\n\n"
            "Ответь СТРОГО одним словом: YES или NO"
        )

        try:
            answer = await self._call_claude(eval_prompt, model="haiku")
            return "YES" in answer.upper()
        except Exception as e:
            logger.error(f"Notification evaluation error: {e}")
            return False

    async def _send_notification(self, trigger_name: str, text: str) -> None:
        """Отправить уведомление через bus."""
        if not self.bus:
            return

        msg = FleetMessage(
            source=f"agent:{self.agent_name}",
            target=f"telegram:{self.agent_name}",
            content=f"[{trigger_name}]\n\n{text}",
            msg_type=MessageType.OUTBOUND,
            chat_id=self.chat_id,
        )
        await self.bus.publish(msg)
        logger.info(
            f"SmartTrigger '{trigger_name}': уведомление отправлено "
            f"в чат {self.chat_id}"
        )

    def _log_to_daily(self, trigger_name: str, response: str) -> None:
        """Записать результат триггера в daily note."""
        try:
            now = datetime.now()
            time_str = now.strftime("%H:%M")
            entry = f"\n### [{time_str}] SmartTrigger: {trigger_name}\n\n{response}\n"
            memory.log_message(
                self.agent_dir,
                role="assistant",
                content=entry,
                date=now,
            )
        except Exception as e:
            logger.error(f"Daily note log error: {e}")

    def _cleanup_last_run(self, now: datetime) -> None:
        """Удалить старые записи из _last_run (старше 1 часа)."""
        current_key_prefix = now.strftime("%Y-%m-%d %H:")
        prev_hour = now.replace(minute=0, second=0)
        prev_key_prefix = prev_hour.strftime("%Y-%m-%d %H:")

        keys_to_remove = [
            k for k in self._last_run
            if not k.split(":", 1)[-1].startswith(current_key_prefix)
            and not k.split(":", 1)[-1].startswith(prev_key_prefix)
        ]
        for k in keys_to_remove:
            del self._last_run[k]
