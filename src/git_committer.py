"""
Async debounced committer для memory-директорий.

Горячий путь (agent.py) делает `git commit` после каждого ответа агента.
Синхронный git блокирует event loop на 50-200 мс (больше на медленном
диске или тяжёлой истории). Этот committer:

1. Оборачивает git_commit в `asyncio.to_thread` — снимает работу с loop.
2. Дебаунсит: несколько commit-запросов в течение `delay` секунд
   объединяются в один коммит. Для агента, который отвечает подряд
   на несколько сообщений, это даёт один snapshot вместо трёх.

На shutdown вызывается `flush()` — все отложенные коммиты выполняются
синхронно перед выходом.
"""

import asyncio
import logging

from . import memory

logger = logging.getLogger(__name__)


class GitCommitter:
    """Debounced async committer для memory-директорий."""

    def __init__(self, delay: float = 2.0) -> None:
        self._delay = delay
        self._pending: dict[str, list[str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def commit(self, agent_dir: str, message: str | None = None) -> None:
        """Запланировать коммит. Возвращает сразу, сам коммит — через `delay` сек."""
        msg = message or "Memory update"
        async with self._lock:
            self._pending.setdefault(agent_dir, []).append(msg)
            existing = self._tasks.get(agent_dir)
            if existing and not existing.done():
                existing.cancel()
            self._tasks[agent_dir] = asyncio.create_task(self._flush_after_delay(agent_dir))

    async def _flush_after_delay(self, agent_dir: str) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return
        await self._flush_one(agent_dir)

    async def _flush_one(self, agent_dir: str) -> None:
        async with self._lock:
            messages = self._pending.pop(agent_dir, [])
            self._tasks.pop(agent_dir, None)
        if not messages:
            return
        if len(messages) == 1:
            final_msg = messages[0]
        else:
            final_msg = f"{messages[0]} (+{len(messages) - 1} more events)"
        try:
            await asyncio.to_thread(memory.git_commit, agent_dir, final_msg)
        except Exception as e:
            logger.warning(f"git_committer: commit for {agent_dir} failed: {e}")

    async def flush(self) -> None:
        """Форсированный flush: выполнить все отложенные коммиты сейчас."""
        async with self._lock:
            pending_dirs = list(self._pending.keys())
            for task in list(self._tasks.values()):
                if not task.done():
                    task.cancel()
            self._tasks.clear()
        for agent_dir in pending_dirs:
            await self._flush_one(agent_dir)


_default: GitCommitter | None = None


def get_committer() -> GitCommitter:
    global _default
    if _default is None:
        _default = GitCommitter()
    return _default


async def commit(agent_dir: str, message: str | None = None) -> None:
    """Удобный модульный вызов: `await git_committer.commit(dir, msg)`."""
    await get_committer().commit(agent_dir, message)


async def flush() -> None:
    """Выполнить все отложенные коммиты. Вызывать на shutdown."""
    await get_committer().flush()
