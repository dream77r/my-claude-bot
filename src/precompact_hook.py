"""
PreCompact hook — снапшот памяти перед сжатием контекста.

Claude Code вызывает PreCompact перед сжатием сессии. Если у агента есть
несохранённые правки в wiki/profile (текущий цикл мог дописать факты), они
рискуют потеряться после компакции — сжатая версия контекста может не
содержать намерения, с которым правки делались. Этот hook делает
`git add -A && git commit` в memory-директории, чтобы зафиксировать
work-in-progress до сжатия.

Дополнительно — дописывает маркер "Компакт сессии" в today's daily note,
чтобы агент после пробуждения понимал, что контекст был сжат.

Вызывается Claude Code как `python precompact_hook.py <memory_path>`.
Никогда не завершается с ошибкой: PreCompact не должен блокировать
компакцию, даже если git не работает.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _run_git(memory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(memory),
        capture_output=True,
        text=True,
        timeout=10,
    )


def snapshot_memory(memory: Path) -> str | None:
    """Закоммитить все незафиксированные правки. Вернуть short sha или None."""
    if not (memory / ".git").is_dir():
        return None

    status = _run_git(memory, "status", "--porcelain")
    if status.returncode != 0 or not status.stdout.strip():
        return None

    add = _run_git(memory, "add", "-A")
    if add.returncode != 0:
        return None

    stamp = datetime.now().strftime("%H:%M")
    commit = _run_git(memory, "commit", "-m", f"Pre-compact snapshot {stamp}", "--quiet")
    if commit.returncode != 0:
        return None

    sha = _run_git(memory, "rev-parse", "--short", "HEAD")
    return sha.stdout.strip() if sha.returncode == 0 else ""


def append_daily_marker(memory: Path, sha: str | None) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    hhmm = datetime.now().strftime("%H:%M")
    daily_dir = memory / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    daily_file = daily_dir / f"{today}.md"

    snapshot_note = f" (snapshot `{sha}`)" if sha else ""
    marker = (
        f"\n## Компакт сессии {hhmm}{snapshot_note}\n"
        f"Контекст сжат. Ключевая информация сохранена в profile.md и wiki/.\n"
    )
    with daily_file.open("a", encoding="utf-8") as f:
        f.write(marker)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(0)

    memory = Path(sys.argv[1]).resolve()
    if not memory.is_dir():
        sys.exit(0)

    try:
        sha = snapshot_memory(memory)
    except Exception:
        sha = None

    try:
        append_daily_marker(memory, sha)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
