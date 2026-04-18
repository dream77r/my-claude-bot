"""
Общие обёртки для вызова git subprocess'ом.

Консолидирует дублирующийся код, который раньше жил в
``memory.py._run_git`` и ``github_sync.py._run_git``. Два вызывающих
контекста имеют разные потребности:

- ``memory.py`` читает ``stdout``/``returncode`` напрямую из
  ``CompletedProcess`` и хочет короткий таймаут (git-операции в локальном
  репо памяти).
- ``github_sync.py`` работает с сетью (clone/pull/push) и хочет длинный
  таймаут, плюс упрощённый ``(ok, output)``-возврат для проверки успеха
  без исключений.

Этот модуль экспонирует:

- :func:`run_git` — низкоуровневая обёртка, возвращает ``CompletedProcess``.
- :func:`run_git_checked` — вариант для сетевых операций: возвращает
  ``(ok, combined_output)``, ловит ``TimeoutExpired`` и общие исключения.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def run_git(
    args: Iterable[str],
    cwd: Path | str,
    timeout: int = 30,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """
    Выполнить git-команду и вернуть ``CompletedProcess``.

    Не подавляет исключения — вызывающий код сам решает что делать с
    ``TimeoutExpired`` и прочими ошибками запуска. Подходит для случаев,
    когда нужен доступ к ``stdout`` (например, разбор вывода ``git log``).

    Args:
        args: аргументы git (без самого слова ``git``), например
            ``("status", "--porcelain")`` или ``["add", "-A"]``.
        cwd: рабочая директория для git-команды.
        timeout: таймаут в секундах. По умолчанию 30 — подходит для
            локальных операций; для сетевых (clone/pull/push)
            используйте более высокое значение.
        capture_output: захватывать ли stdout/stderr.
        text: декодировать ли вывод как текст.

    Returns:
        subprocess.CompletedProcess — результат запуска.
    """
    return subprocess.run(
        ["git", *list(args)],
        cwd=str(cwd),
        capture_output=capture_output,
        text=text,
        timeout=timeout,
    )


def run_git_checked(
    args: list[str],
    cwd: Path | str,
    timeout: int = 120,
) -> tuple[bool, str]:
    """
    Выполнить git-команду, поглотив исключения, и вернуть ``(ok, output)``.

    Используется там, где нужен простой булевый индикатор успеха плюс
    комбинированный stdout+stderr для ручной проверки (например,
    "nothing to commit" или "empty repository"). Ловит
    ``TimeoutExpired`` и общие исключения, логируя их через
    ``logger.error``; при ненулевом ``returncode`` пишет отладочный лог
    с stderr.

    Args:
        args: аргументы git (без самого слова ``git``).
        cwd: рабочая директория.
        timeout: таймаут в секундах. По умолчанию 120 — рассчитано на
            сетевые операции.

    Returns:
        (ok, output): ``ok=True`` если ``returncode == 0``; ``output`` —
        склейка stdout+stderr (или ``"timeout"``/текст исключения при
        провале).
    """
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            logger.debug(f"git {' '.join(args)}: {result.stderr.strip()}")
            return False, output
        return True, output
    except subprocess.TimeoutExpired:
        logger.error(f"git {' '.join(args)}: timeout")
        return False, "timeout"
    except Exception as e:
        logger.error(f"git {' '.join(args)}: {e}")
        return False, str(e)
