"""
SSRF Protection — блокировка запросов к внутренним адресам.

Защита от Server-Side Request Forgery:
- Блокирует fetch к приватным IP-диапазонам (RFC 1918)
- Блокирует loopback, link-local, CGNAT
- DNS resolution → IP check → fail-closed при DNS failure
- Whitelist для исключений (например, Tailscale)

Два режима:
1. on_tool_use хук — проверка URL в WebFetch/WebSearch tool calls
2. Функция validate_url() для прямых проверок

По мотивам nanobot/security/network.py (HKUDS).
"""

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# Заблокированные IP-диапазоны
_BLOCKED_NETWORKS = [
    # Loopback
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),

    # Private (RFC 1918)
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),

    # Link-local
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),

    # CGNAT (Carrier-grade NAT)
    ipaddress.ip_network("100.64.0.0/10"),

    # IPv6 Unique Local Address
    ipaddress.ip_network("fc00::/7"),

    # IPv4-mapped IPv6
    ipaddress.ip_network("::ffff:127.0.0.0/104"),
    ipaddress.ip_network("::ffff:10.0.0.0/104"),
    ipaddress.ip_network("::ffff:172.16.0.0/108"),
    ipaddress.ip_network("::ffff:192.168.0.0/112"),
]

# Подозрительные hostname паттерны
_SUSPICIOUS_HOSTNAMES = re.compile(
    r"(localhost|0\.0\.0\.0|internal|intranet|"
    r"metadata\.google|169\.254\.\d+\.\d+|"
    r"\[::1?\])",
    re.IGNORECASE,
)

# Разрешённые схемы
_ALLOWED_SCHEMES = {"http", "https"}


def _is_ip_blocked(ip_str: str) -> bool:
    """Проверить, попадает ли IP в заблокированные диапазоны."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Невалидный IP = блокируем (fail-closed)

    for network in _BLOCKED_NETWORKS:
        if addr in network:
            return True
    return False


def _resolve_hostname(hostname: str) -> list[str]:
    """
    Разрешить hostname в список IP-адресов.

    При ошибке DNS = пустой список (fail-closed).
    """
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
        return list({r[4][0] for r in results})
    except (socket.gaierror, OSError) as e:
        logger.warning(f"SSRF: DNS resolution failed for '{hostname}': {e}")
        return []


def validate_url(url: str, whitelist: list[str] | None = None) -> tuple[bool, str]:
    """
    Проверить URL на SSRF.

    Args:
        url: URL для проверки
        whitelist: список CIDR для исключений (например, ["100.64.0.0/10"])

    Returns:
        (is_safe, reason) — True если URL безопасен
    """
    # Парсинг URL
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "невалидный URL"

    # Проверка схемы
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False, f"запрещённая схема: {parsed.scheme}"

    # Проверка hostname
    hostname = parsed.hostname
    if not hostname:
        return False, "отсутствует hostname"

    # Подозрительные hostname
    if _SUSPICIOUS_HOSTNAMES.search(hostname):
        return False, f"подозрительный hostname: {hostname}"

    # Проверка: hostname уже является IP?
    try:
        ip = ipaddress.ip_address(hostname)
        if _is_ip_blocked(str(ip)):
            # Проверить whitelist
            if whitelist and _is_whitelisted(str(ip), whitelist):
                return True, "whitelisted"
            return False, f"заблокированный IP: {ip}"
        return True, "ok"
    except ValueError:
        pass  # Не IP — нужен DNS resolve

    # DNS resolution
    ips = _resolve_hostname(hostname)
    if not ips:
        return False, f"DNS resolution failed для '{hostname}' (fail-closed)"

    # Проверить каждый resolved IP
    for ip_str in ips:
        if _is_ip_blocked(ip_str):
            if whitelist and _is_whitelisted(ip_str, whitelist):
                continue
            return False, f"hostname '{hostname}' разрешается в заблокированный IP: {ip_str}"

    return True, "ok"


def _is_whitelisted(ip_str: str, whitelist: list[str]) -> bool:
    """Проверить, попадает ли IP в whitelist CIDR."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    for cidr in whitelist:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            if addr in network:
                return True
        except ValueError:
            continue
    return False


def contains_internal_url(text: str) -> list[str]:
    """
    Найти URL к внутренним адресам в тексте команды.

    Используется для проверки shell-команд (curl, wget, etc.).

    Returns:
        Список обнаруженных внутренних URL.
    """
    url_pattern = re.compile(r"https?://[^\s'\"]+")
    findings = []

    for match in url_pattern.finditer(text):
        url = match.group(0)
        is_safe, reason = validate_url(url)
        if not is_safe:
            findings.append(f"{url} ({reason})")

    return findings


def make_ssrf_hook(whitelist: list[str] | None = None):
    """
    Создать on_tool_use хук для Hook-системы.

    Проверяет URL в:
    - WebFetch tool — параметр url
    - Bash tool — поиск URL в тексте команды (curl, wget)
    """
    from .hooks import HookContext

    async def _ssrf_hook(ctx: HookContext) -> HookContext:
        tool_name = ctx.data.get("tool_name", "")
        tool_input = ctx.data.get("tool_input", {})

        if tool_name == "WebFetch":
            url = tool_input.get("url", "")
            if url:
                is_safe, reason = validate_url(url, whitelist)
                if not is_safe:
                    logger.warning(
                        f"SSRF blocked: WebFetch к '{url}' — {reason}"
                    )
                    ctx.data["ssrf_blocked"] = True
                    ctx.data["ssrf_reason"] = reason

        elif tool_name == "Bash":
            command = tool_input.get("command", "")
            internal_urls = contains_internal_url(command)
            if internal_urls:
                logger.warning(
                    f"SSRF warning: Bash содержит внутренние URL: "
                    f"{', '.join(internal_urls)}"
                )
                ctx.data["ssrf_warning"] = internal_urls

        return ctx

    return _ssrf_hook
