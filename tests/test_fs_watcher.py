"""Tests for src/fs_watcher.DirectoryWatcher."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from src import fs_watcher
from src.fs_watcher import DirectoryWatcher


@pytest.mark.asyncio
async def test_start_creates_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "dispatch"
    assert not target.exists()

    watcher = DirectoryWatcher(target)
    try:
        watcher.start()
        assert target.is_dir()
    finally:
        watcher.stop()


@pytest.mark.asyncio
async def test_wait_returns_after_timeout_when_no_events(tmp_path: Path) -> None:
    watcher = DirectoryWatcher(tmp_path)
    watcher.start()
    try:
        start = time.monotonic()
        await watcher.wait(timeout=0.2)
        elapsed = time.monotonic() - start
        assert 0.15 <= elapsed < 1.0, f"wait elapsed={elapsed:.3f}s"
    finally:
        watcher.stop()


@pytest.mark.asyncio
async def test_inotify_wakes_promptly_on_file_create(tmp_path: Path) -> None:
    watcher = DirectoryWatcher(tmp_path)
    watcher.start()
    if watcher.mode != "inotify":
        watcher.stop()
        pytest.skip("watchdog/inotify not available on this host")

    try:
        async def create_soon() -> None:
            await asyncio.sleep(0.1)
            (tmp_path / "hello.json").write_text("{}", encoding="utf-8")

        asyncio.create_task(create_soon())
        start = time.monotonic()
        # Long timeout — we expect to wake well before it hits.
        await watcher.wait(timeout=5.0)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"wait should wake on fs event, elapsed={elapsed:.3f}s"
    finally:
        watcher.stop()


@pytest.mark.asyncio
async def test_inotify_consecutive_events_each_wake_wait(tmp_path: Path) -> None:
    """Two separate file creations should each wake separate wait() calls."""
    watcher = DirectoryWatcher(tmp_path)
    watcher.start()
    if watcher.mode != "inotify":
        watcher.stop()
        pytest.skip("watchdog/inotify not available on this host")

    try:
        # First event
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        start = time.monotonic()
        await watcher.wait(timeout=2.0)
        assert time.monotonic() - start < 1.5

        # Second event — wait should wake again after new file
        async def create_second() -> None:
            await asyncio.sleep(0.1)
            (tmp_path / "b.json").write_text("{}", encoding="utf-8")

        asyncio.create_task(create_second())
        start = time.monotonic()
        await watcher.wait(timeout=2.0)
        assert time.monotonic() - start < 1.5
    finally:
        watcher.stop()


@pytest.mark.asyncio
async def test_polling_fallback_when_watchdog_import_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If watchdog can't be imported, start() should fall back cleanly."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("watchdog"):
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    watcher = DirectoryWatcher(tmp_path)
    watcher.start()
    try:
        assert watcher.mode == "polling"

        start = time.monotonic()
        await watcher.wait(timeout=0.1)
        elapsed = time.monotonic() - start
        # Polling just sleeps — even creating a file during the sleep
        # must not shorten it.
        assert elapsed >= 0.1
    finally:
        watcher.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path: Path) -> None:
    watcher = DirectoryWatcher(tmp_path)
    watcher.start()
    watcher.stop()
    # Second stop must not raise even though observer is already gone.
    watcher.stop()


@pytest.mark.asyncio
async def test_stop_without_start_is_safe(tmp_path: Path) -> None:
    watcher = DirectoryWatcher(tmp_path)
    watcher.stop()  # never started


@pytest.mark.asyncio
async def test_polling_mode_before_start(tmp_path: Path) -> None:
    """Before start(), wait() should still work (defensive default)."""
    watcher = DirectoryWatcher(tmp_path)
    assert watcher.mode == "polling"
    start = time.monotonic()
    await watcher.wait(timeout=0.1)
    assert time.monotonic() - start >= 0.1
