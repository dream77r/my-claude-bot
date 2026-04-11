"""
Command Router — 4-уровневая маршрутизация команд.

Уровни приоритета:
1. Priority: /stop — работают даже когда агент занят
2. Exact: /help, /newsession, /status — точное совпадение
3. Prefix: /team_coder задача — маршрутизация по префиксу (Phase 2)
4. Interceptors: fallback-предикаты (Phase 2)

Критично: пользователь может остановить зависшего агента через /stop.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Тип хэндлера команды
CommandHandler_t = Callable[
    [Update, ContextTypes.DEFAULT_TYPE, str],
    Coroutine[Any, Any, None],
]


@dataclass
class RouteResult:
    """Результат маршрутизации."""

    handler: CommandHandler_t
    args: str
    is_priority: bool = False


class CommandRouter:
    """
    4-уровневый роутер команд.

    Priority-команды (например /stop) выполняются немедленно,
    даже если агент сейчас обрабатывает запрос.
    """

    def __init__(self):
        self._priority: dict[str, CommandHandler_t] = {}
        self._exact: dict[str, CommandHandler_t] = {}
        self._prefix: dict[str, CommandHandler_t] = {}
        self._interceptors: list[
            tuple[Callable[[str], bool], CommandHandler_t]
        ] = []

    def priority(self, command: str, handler: CommandHandler_t) -> None:
        """Зарегистрировать priority-команду (работает даже при занятом агенте)."""
        self._priority[command.lower()] = handler

    def exact(self, command: str, handler: CommandHandler_t) -> None:
        """Зарегистрировать exact-match команду."""
        self._exact[command.lower()] = handler

    def prefix(self, command: str, handler: CommandHandler_t) -> None:
        """Зарегистрировать prefix-команду (Phase 2)."""
        self._prefix[command.lower()] = handler

    def interceptor(
        self, predicate: Callable[[str], bool], handler: CommandHandler_t
    ) -> None:
        """Зарегистрировать interceptor (Phase 2)."""
        self._interceptors.append((predicate, handler))

    def route(self, text: str) -> RouteResult | None:
        """
        Найти хэндлер для текста команды.

        Returns:
            RouteResult с хэндлером и аргументами, или None если не найден.
        """
        if not text or not text.startswith("/"):
            return None

        # Убрать @botname из команды (/help@mybot → /help)
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # 1. Priority
        if cmd in self._priority:
            return RouteResult(
                handler=self._priority[cmd], args=args, is_priority=True
            )

        # 2. Exact
        if cmd in self._exact:
            return RouteResult(handler=self._exact[cmd], args=args)

        # 3. Prefix (longest match first)
        best_prefix = ""
        best_handler = None
        for pfx, handler in self._prefix.items():
            if cmd.startswith(pfx) and len(pfx) > len(best_prefix):
                best_prefix = pfx
                best_handler = handler
        if best_handler:
            rest = cmd[len(best_prefix):]
            full_args = (rest + " " + args).strip() if rest else args
            return RouteResult(handler=best_handler, args=full_args)

        # 4. Interceptors
        for predicate, handler in self._interceptors:
            if predicate(text):
                return RouteResult(handler=handler, args=text)

        return None
