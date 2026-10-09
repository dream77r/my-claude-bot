"""
Общий async-раннер для CLI-бэкендов (codex / kimi / mimo).

Каждый адаптер строит argv и передаёт on_stdout_line — async-колбэк,
разбирающий одну строку вывода (JSONL или текст). Раннер занимается
процессом: spawn с отдельной process group, построчное чтение stdout,
таймаут с убийством процесса, сбор хвоста stderr для диагностики.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

from .base import BackendError

STDERR_TAIL_LINES = 30


class CliNotFoundError(BackendError):
    def __init__(self, command: str, env_var: str):
        super().__init__(
            f"Бэкенд недоступен: команда «{command}» не найдена. "
            f"Установите CLI или задайте путь через env {env_var}."
        )
        self.command = command
        self.env_var = env_var


class CliRunError(BackendError):
    def __init__(self, command: str, returncode: int, stderr_tail: str):
        super().__init__(
            f"«{command}» завершился с кодом {returncode}."
            + (f"\nstderr: {stderr_tail}" if stderr_tail else "")
        )
        self.command = command
        self.returncode = returncode
        self.stderr_tail = stderr_tail


def resolve_cli_path(command: str, env_var: str) -> str:
    """Путь к бинарю: env-override → PATH. CliNotFoundError если нигде нет."""
    import os

    override = os.environ.get(env_var)
    if override:
        p = Path(override)
        if p.exists():
            return str(p)
        found = shutil.which(override)
        if found:
            return found
        raise CliNotFoundError(override, env_var)
    found = shutil.which(command)
    if found:
        return found
    raise CliNotFoundError(command, env_var)


OnLine = Callable[[str], Awaitable[None]]


async def run_cli(
    *,
    argv: list[str],
    cwd: str | Path,
    timeout: float,
    on_stdout_line: OnLine,
    display_name: str,
) -> None:
    """
    Запустить CLI, прочитать stdout построчно, дождаться завершения.

    Raises:
        CliRunError: ненулевой exit code (stderr-хвост внутри).
        BackendError: процесс не запустился.
        asyncio.TimeoutError: таймаут, процесс убит.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # своя process group для полного убийства по таймауту
    )

    stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)

    async def _pump_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

    async def _pump_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            await on_stdout_line(
                line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            )

    try:
        await asyncio.wait_for(
            asyncio.gather(_pump_stdout(), _pump_stderr(), proc.wait()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        raise asyncio.TimeoutError(
            f"«{display_name}» не завершился за {timeout:.0f}с, процесс убит"
        )
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    if proc.returncode != 0:
        raise CliRunError(
            display_name,
            proc.returncode,
            "\n".join(stderr_tail).strip(),
        )


def compose_prompt(system_prompt: str | None, prompt: str) -> str:
    """Склеить system prompt и задачу для CLI без нативного --system флага."""
    if system_prompt:
        return f"{system_prompt}\n\n---\n\n{prompt}"
    return prompt


def extract_content_text(content) -> str:
    """
    Текст из content сообщения в духе chat-completions: строка или список
    частей [{"type": "text", "text": ...}]. Неизвестные части пропускаем.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "text" and isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(p for p in parts if p)
    return ""


def extract_tool_names(obj: dict) -> list[str]:
    """Имена инструментов из JSONL-события (tool_calls в разных формах)."""
    names: list[str] = []
    candidates = [
        obj.get("tool_calls"),
        (obj.get("message") or {}).get("tool_calls") if isinstance(obj.get("message"), dict) else None,
        obj.get("tools"),
    ]
    for calls in candidates:
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict):
                name = call.get("name")
                if name is None and isinstance(call.get("function"), dict):
                    name = call["function"].get("name")
                if isinstance(name, str) and name:
                    names.append(name)
    return names
