"""
Event-driven directory watcher with polling fallback.

Replaces the `while True: sleep(N); glob(...)` pattern used by
dispatcher/delegation loops. When `watchdog` + inotify (or the platform
equivalent) is available, filesystem events wake the loop in ~ms; the
`timeout` argument then acts as a safety-net scan interval. When
`watchdog` can't start, `wait()` degrades to plain `asyncio.sleep`, so
callers behave exactly as before.

Usage:

    watcher = DirectoryWatcher(dir_path)
    watcher.start()
    try:
        while True:
            await watcher.wait(timeout=5.0)
            for path in dir_path.glob("*.json"):
                ...
    finally:
        watcher.stop()
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class DirectoryWatcher:
    """Wake an asyncio coroutine on filesystem events in a directory.

    The watcher itself is not recursive — it watches exactly `directory`.
    Events from any file in that directory (create, move, modify) set an
    internal asyncio.Event that `wait()` consumes.
    """

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self._event = asyncio.Event()
        self._observer = None
        self._mode = "polling"

    @property
    def mode(self) -> str:
        """`"inotify"` when watchdog is running, `"polling"` otherwise."""
        return self._mode

    def start(self) -> None:
        """Start watching. Safe to call when watchdog is unavailable.

        Requires a running event loop — the watchdog thread posts wakeups
        back onto it via `loop.call_soon_threadsafe`.
        """
        self.directory.mkdir(parents=True, exist_ok=True)

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as e:
            logger.info(
                f"DirectoryWatcher: watchdog not installed, using polling "
                f"for {self.directory} ({e})"
            )
            self._mode = "polling"
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as e:
            logger.warning(
                f"DirectoryWatcher.start: no running loop, using polling "
                f"for {self.directory} ({e})"
            )
            self._mode = "polling"
            return

        event = self._event

        class _Handler(FileSystemEventHandler):
            def _notify(self) -> None:
                try:
                    loop.call_soon_threadsafe(event.set)
                except RuntimeError:
                    pass

            def on_created(self, _ev):
                self._notify()

            def on_moved(self, _ev):
                self._notify()

            def on_modified(self, _ev):
                self._notify()

        try:
            observer = Observer()
            observer.schedule(_Handler(), str(self.directory), recursive=False)
            observer.start()
        except Exception as e:
            logger.warning(
                f"DirectoryWatcher: observer.start failed for "
                f"{self.directory}, using polling "
                f"({type(e).__name__}: {e})"
            )
            self._mode = "polling"
            return

        self._observer = observer
        self._mode = "inotify"
        logger.info(f"DirectoryWatcher: inotify mode for {self.directory}")

    def stop(self) -> None:
        """Stop the watchdog thread. Safe to call when not started."""
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=2)
        except Exception as e:
            logger.warning(f"DirectoryWatcher.stop: {e}")
        self._observer = None

    async def wait(self, timeout: float) -> None:
        """Sleep until a filesystem event arrives or `timeout` elapses.

        In polling mode this is equivalent to `asyncio.sleep(timeout)`.
        In inotify mode it returns as soon as any event fires in the
        watched directory, but never waits longer than `timeout` so the
        caller still runs a safety-net scan periodically.
        """
        if self._mode != "inotify":
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._event.clear()
