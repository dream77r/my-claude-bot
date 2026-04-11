"""
Hook-система — lifecycle hooks для расширяемости агентов.

Точки расширения в жизненном цикле агента без модификации ядра:
- before_call — перед вызовом Claude (модификация промпта, логирование)
- after_call — после ответа Claude (метрики, постобработка)
- on_tool_use — при использовании инструмента (аудит, ограничения)
- on_error — при ошибке (алертинг, fallback)

Каждый хук получает HookContext и может модифицировать его data.
CompositeHook обеспечивает error isolation — сломанный хук не роняет цикл.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HookContext:
    """Контекст, передаваемый в хук."""
    event: str
    agent_name: str
    data: dict[str, Any] = field(default_factory=dict)


class Hook:
    """Базовый хук."""

    def __init__(self, name: str):
        self.name = name

    async def execute(self, ctx: HookContext) -> HookContext:
        """Выполнить хук. Переопределяется в подклассах."""
        return ctx


class FunctionHook(Hook):
    """Хук из async-функции."""

    def __init__(self, name: str, fn):
        super().__init__(name)
        self._fn = fn

    async def execute(self, ctx: HookContext) -> HookContext:
        return await self._fn(ctx)


class HookRegistry:
    """
    Реестр хуков с error isolation.

    Каждый хук выполняется изолированно — исключение в одном хуке
    не останавливает остальные и не роняет основной цикл агента.
    """

    def __init__(self):
        self._hooks: dict[str, list[Hook]] = defaultdict(list)

    def register(self, event: str, hook: Hook) -> None:
        """Зарегистрировать хук на событие."""
        self._hooks[event].append(hook)
        logger.debug(f"Hook '{hook.name}' зарегистрирован на '{event}'")

    def register_fn(self, event: str, name: str, fn) -> None:
        """Зарегистрировать async-функцию как хук."""
        self.register(event, FunctionHook(name, fn))

    async def emit(self, event: str, ctx: HookContext) -> HookContext:
        """
        Вызвать все хуки для события.

        Хуки выполняются последовательно, каждый получает контекст
        от предыдущего. Ошибка в хуке логируется, но не прерывает цепочку.

        Returns:
            Модифицированный контекст после всех хуков.
        """
        hooks = self._hooks.get(event, [])
        for hook in hooks:
            try:
                ctx = await hook.execute(ctx)
            except Exception as e:
                logger.error(
                    f"Hook '{hook.name}' error on '{event}': {e}",
                    exc_info=True,
                )
        return ctx

    @property
    def events(self) -> list[str]:
        """Список зарегистрированных событий."""
        return list(self._hooks.keys())

    def count(self, event: str) -> int:
        """Количество хуков на событие."""
        return len(self._hooks.get(event, []))
