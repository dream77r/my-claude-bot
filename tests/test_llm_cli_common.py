"""Тесты общего CLI-слоя: парсеры событий, resolve_cli_path, run_cli."""

import asyncio
import os
import stat
import sys
from unittest.mock import MagicMock

import pytest

from src.llm.cli_common import (
    CliNotFoundError,
    CliRunError,
    extract_content_text,
    extract_tool_names,
    resolve_cli_path,
    run_cli,
)
from src.llm.codex_cli import _CodexStream
from src.llm.kimi_cli import _KimiStream
from src.llm.mimo_cli import _MimoStream

# ── extract_content_text ──


def test_extract_content_text_plain_string():
    assert extract_content_text("привет") == "привет"


def test_extract_content_text_list_of_text_parts():
    content = [
        {"type": "text", "text": "часть 1"},
        {"type": "text", "text": "часть 2"},
    ]
    assert extract_content_text(content) == "часть 1\nчасть 2"


def test_extract_content_text_list_of_content_key_parts():
    content = [{"type": "text", "content": "текст части"}]
    assert extract_content_text(content) == "текст части"


def test_extract_content_text_skips_unknown_parts():
    content = [
        {"type": "image"},
        "не-dict элемент",
        {"text": None},
        {"type": "text", "text": "только это"},
    ]
    assert extract_content_text(content) == "только это"


def test_extract_content_text_garbage_returns_empty():
    for garbage in (None, 42, {"not": "a list"}, [None, 1]):
        assert extract_content_text(garbage) == ""


# ── extract_tool_names ──


def test_extract_tool_names_top_level():
    obj = {"tool_calls": [{"name": "Read"}, {"name": "Write"}]}
    assert extract_tool_names(obj) == ["Read", "Write"]


def test_extract_tool_names_nested_under_function():
    obj = {"tool_calls": [{"function": {"name": "Bash"}}]}
    assert extract_tool_names(obj) == ["Bash"]


def test_extract_tool_names_inside_message():
    obj = {"message": {"role": "assistant", "tool_calls": [{"name": "Grep"}]}}
    assert extract_tool_names(obj) == ["Grep"]


def test_extract_tool_names_absent_returns_empty():
    assert extract_tool_names({"type": "agent_message"}) == []
    assert extract_tool_names({"tool_calls": "не-список"}) == []


# ── парсеры адаптеров ──


class _DeltaRecorder:
    """on_text_delta-колбэк: запоминает каждый снапшот текста."""

    def __init__(self):
        self.snapshots = []

    async def __call__(self, text: str) -> None:
        self.snapshots.append(text)


def _kimi_sample_lines():
    return [
        # Полное сообщение со строковым content
        '{"message": {"role": "assistant", "content": "Начало ответа."}}',
        # Дельта-текст
        '{"delta": " продолжение"}',
        # Списочный content
        '{"message": {"role": "assistant", "content": [{"type": "text", "text": "часть A"}, {"type": "text", "text": "часть B"}]}}',
        # Событие с инструментом
        '{"tool_calls": [{"function": {"name": "Read"}}]}',
        # Мусор: пустая строка, не-JSON, JSON не-объект
        "",
        "не-json строка",
        "[1, 2, 3]",
    ]


@pytest.mark.asyncio
async def test_kimi_stream_accumulates_text_and_emits_deltas():
    recorder = _DeltaRecorder()
    on_tool_use = MagicMock()

    async def _tool(hint):
        on_tool_use(hint)

    stream = _KimiStream(recorder, _tool)
    for line in _kimi_sample_lines():
        await stream.on_line(line)

    assert "Начало ответа." in stream.text
    assert "продолжение" in stream.text
    assert "часть A" in stream.text and "часть B" in stream.text
    on_tool_use.assert_called_once_with("🔧 Read")

    # on_text_delta получал растущий текст
    assert len(recorder.snapshots) == 3  # три текстовых события
    assert recorder.snapshots[0].strip() == "Начало ответа."
    for prev, nxt in zip(recorder.snapshots, recorder.snapshots[1:]):
        assert len(nxt) > len(prev)


def _codex_sample_lines():
    return [
        '{"message": {"role": "assistant", "content": "Итог по задаче."}}',
        '{"delta": " дописываю"}',
        '{"text": " финальная строка"}',
        '{"tool_calls": [{"name": "shell"}]}',
        '{"type": "turn.completed"}',
    ]


@pytest.mark.asyncio
async def test_codex_stream_accumulates_text_and_emits_deltas():
    recorder = _DeltaRecorder()
    tool_hints = []

    async def _tool(hint):
        tool_hints.append(hint)

    stream = _CodexStream(recorder, _tool)
    for line in _codex_sample_lines():
        await stream.on_line(line)

    assert "Итог по задаче." in stream.text
    assert "дописываю" in stream.text
    assert "финальная строка" in stream.text
    # turn.completed текста не несёт — дельт было три
    assert tool_hints == ["🔧 shell"]
    assert len(recorder.snapshots) == 3
    assert recorder.snapshots[-1].strip().endswith("финальная строка")


@pytest.mark.asyncio
async def test_mimo_stream_strips_bullets_and_skips_blanks():
    recorder = _DeltaRecorder()
    stream = _MimoStream(recorder)

    await stream.on_line("• первая строка")
    await stream.on_line("")
    await stream.on_line("   ")
    await stream.on_line("•вторая без пробела")
    await stream.on_line("обычная строка без маркера")

    assert stream.lines == [
        "первая строка",
        "вторая без пробела",
        "обычная строка без маркера",
    ]
    # Снапшоты растут и склеены переносами
    assert recorder.snapshots[0] == "первая строка"
    assert recorder.snapshots[-1] == "\n".join(stream.lines)


# ── resolve_cli_path ──


def test_resolve_cli_path_env_override_existing_file(tmp_path, monkeypatch):
    fake = tmp_path / "mycli"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("TEST_MYKLI_PATH", str(fake))
    assert resolve_cli_path("whatever", "TEST_MYKLI_PATH") == str(fake)


def test_resolve_cli_path_env_wins_over_path(tmp_path, monkeypatch):
    """Env-override к существующему файлу побеждает одноимённый бинарь в PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "python3"
    shim.write_text("#!/bin/sh\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))

    override = tmp_path / "override"
    override.write_text("#!/bin/sh\n")
    monkeypatch.setenv("TEST_OVERRIDE_PATH", str(override))

    assert resolve_cli_path("python3", "TEST_OVERRIDE_PATH") == str(override)


def test_resolve_cli_path_falls_back_to_which(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "mycli"
    shim.write_text("#!/bin/sh\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("TEST_UNUSED_ENV_VAR", raising=False)

    assert resolve_cli_path("mycli", "TEST_UNUSED_ENV_VAR") == str(shim)


def test_resolve_cli_path_raises_when_nowhere(monkeypatch):
    monkeypatch.delenv("TEST_GHOST_CLI_PATH", raising=False)
    with pytest.raises(CliNotFoundError):
        resolve_cli_path("definitely-not-a-real-cli-xyz", "TEST_GHOST_CLI_PATH")


# ── run_cli ──


@pytest.mark.asyncio
async def test_run_cli_delivers_lines_in_order(tmp_path):
    lines = []

    async def on_line(line: str) -> None:
        lines.append(line)

    await run_cli(
        argv=[sys.executable, "-c", "print('one'); print('two'); print('three')"],
        cwd=tmp_path,
        timeout=10,
        on_stdout_line=on_line,
        display_name="test",
    )
    assert lines == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_run_cli_nonzero_exit_raises_with_stderr_tail(tmp_path):
    async def on_line(line: str) -> None:
        pass

    with pytest.raises(CliRunError) as exc_info:
        await run_cli(
            argv=[
                sys.executable, "-c",
                "import sys; sys.stderr.write('boom-detail\\n'); sys.exit(3)",
            ],
            cwd=tmp_path,
            timeout=10,
            on_stdout_line=on_line,
            display_name="test-cli",
        )
    err = exc_info.value
    assert err.returncode == 3
    assert "boom-detail" in err.stderr_tail
    assert "boom-detail" in str(err)


@pytest.mark.asyncio
async def test_run_cli_missing_binary_raises_oserror(tmp_path):
    async def on_line(line: str) -> None:
        pass

    with pytest.raises(OSError):
        await run_cli(
            argv=["/nonexistent/definitely-not-a-binary-xyz"],
            cwd=tmp_path,
            timeout=10,
            on_stdout_line=on_line,
            display_name="test",
        )


@pytest.mark.asyncio
async def test_run_cli_timeout_kills_process(tmp_path):
    """Таймаут → asyncio.TimeoutError, процесс прибит, зомби не остаётся."""
    captured = []

    async def on_line(line: str) -> None:
        captured.append(line)

    with pytest.raises(asyncio.TimeoutError) as exc_info:
        await run_cli(
            argv=[
                sys.executable, "-c",
                "import os,time; print(os.getpid(), flush=True); time.sleep(30)",
            ],
            cwd=tmp_path,
            timeout=0.3,
            on_stdout_line=on_line,
            display_name="sleepy",
        )

    assert "процесс убит" in str(exc_info.value)
    pid = int(captured[0])

    # SIGKILL уже отправлен — дожидаёмся реапинга (ThreadedChildWatcher
    # на Linux реапит автоматически) и проверяем что процесса нет.
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"процесс {pid} всё ещё жив после таймаута run_cli")
