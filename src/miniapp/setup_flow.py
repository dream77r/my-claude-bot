"""One-click ``/setup_dashboard`` flow — helpers used by TelegramBridge.

This module is intentionally free of Telegram-specific imports so its parts
can be unit-tested without a full bot around them. Privileged work (nginx
config, certbot, reload) is delegated to ``scripts/setup-dashboard.sh``
through sudo with a NOPASSWD whitelist rule.
"""
from __future__ import annotations

import asyncio
import re
import socket
from pathlib import Path


_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
HELPER_SCRIPT = PROJECT_ROOT / "scripts" / "setup-dashboard.sh"
ENV_PATH = PROJECT_ROOT / ".env"


def validate_domain(s: str) -> str:
    """Return a lowercase hostname; raise ValueError on bad input."""
    if not s:
        raise ValueError("domain is empty")
    d = s.strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(d):
        raise ValueError(f"invalid domain: '{s}'")
    return d


def validate_email(s: str) -> str:
    """Return the trimmed email; raise ValueError on bad input."""
    s = s.strip()
    if not _EMAIL_RE.match(s):
        raise ValueError(f"invalid email: '{s}'")
    return s


async def check_dns(domain: str) -> str:
    """Resolve ``domain`` to an IPv4 address; raise ValueError on failure."""
    loop = asyncio.get_event_loop()
    try:
        ip = await loop.run_in_executor(None, socket.gethostbyname, domain)
    except OSError as e:
        raise ValueError(f"DNS lookup failed for {domain}: {e}") from e
    return ip


def pick_free_port(
    start: int = 8080, end: int = 8099, skip_busy: bool = True
) -> int:
    """Pick a TCP port in ``[start, end]`` that can bind on 127.0.0.1."""
    for port in range(start, end + 1):
        if not skip_busy:
            return port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {start}..{end}")


async def sudoers_ready() -> bool:
    """True if ``sudo -n true`` succeeds (NOPASSWD is active)."""
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "true",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def update_env_file(path: str | Path, updates: dict[str, str]) -> None:
    """Idempotently set ``KEY=VALUE`` lines in a dotenv file.

    Existing keys are replaced in place; missing keys are appended. Comments,
    order and unrelated keys are preserved.
    """
    p = Path(path)
    original = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = original.splitlines()
    trailing_nl = original.endswith("\n") if original else True

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        m = _KEY_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    if remaining:
        if out and out[-1] != "":
            out.append("")
        for key, val in remaining.items():
            out.append(f"{key}={val}")

    text = "\n".join(out)
    if trailing_nl and not text.endswith("\n"):
        text += "\n"
    p.write_text(text, encoding="utf-8")


async def run_setup_helper(
    domain: str, port: int, email: str | None = None,
) -> tuple[bool, str]:
    """Run ``sudo scripts/setup-dashboard.sh <domain> <port> [email]``.

    Returns ``(ok, combined_output)`` where ``ok`` is ``True`` iff the helper
    exited with code 0.
    """
    cmd = ["sudo", "-n", str(HELPER_SCRIPT), domain, str(port)]
    if email:
        cmd.append(email)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode == 0, (out or b"").decode("utf-8", errors="replace")


async def trigger_restart_detached(unit: str = "my-claude-bot") -> None:
    """Schedule a restart 2 s from now so the bot's reply lands first."""
    cmd = [
        "systemd-run", "--user",
        "--on-active=2s",
        "systemctl", "--user", "restart", unit,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
