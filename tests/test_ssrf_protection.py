"""Тесты для SSRF Protection — блокировка запросов к внутренним адресам."""

import pytest

from src.ssrf_protection import (
    contains_internal_url,
    make_ssrf_hook,
    validate_url,
)


class TestValidateUrl:
    """Проверка URL на SSRF."""

    # Безопасные URL
    def test_https_external(self):
        ok, _ = validate_url("https://example.com/api")
        assert ok

    def test_http_external(self):
        ok, _ = validate_url("http://example.com")
        assert ok

    # Заблокированные схемы
    def test_ftp_blocked(self):
        ok, reason = validate_url("ftp://example.com/file")
        assert not ok
        assert "схема" in reason

    def test_file_blocked(self):
        ok, reason = validate_url("file:///etc/passwd")
        assert not ok
        assert "схема" in reason

    def test_gopher_blocked(self):
        ok, reason = validate_url("gopher://internal")
        assert not ok

    # Loopback
    def test_localhost_blocked(self):
        ok, reason = validate_url("http://localhost/admin")
        assert not ok
        assert "подозрительный" in reason

    def test_127_0_0_1_blocked(self):
        ok, reason = validate_url("http://127.0.0.1:8080/")
        assert not ok
        assert "заблокированный IP" in reason

    def test_ipv6_loopback_blocked(self):
        ok, reason = validate_url("http://[::1]/")
        assert not ok

    # Private ranges
    def test_10_x_blocked(self):
        ok, reason = validate_url("http://10.0.0.1/")
        assert not ok
        assert "заблокированный" in reason

    def test_172_16_blocked(self):
        ok, reason = validate_url("http://172.16.0.1/")
        assert not ok

    def test_192_168_blocked(self):
        ok, reason = validate_url("http://192.168.1.1/admin")
        assert not ok

    # Link-local
    def test_169_254_blocked(self):
        ok, reason = validate_url("http://169.254.169.254/latest/meta-data/")
        assert not ok

    # CGNAT
    def test_cgnat_blocked(self):
        ok, reason = validate_url("http://100.64.0.1/")
        assert not ok

    # Подозрительные hostnames
    def test_metadata_google_blocked(self):
        ok, reason = validate_url("http://metadata.google.internal/computeMetadata/v1/")
        assert not ok
        assert "подозрительный" in reason

    def test_internal_hostname(self):
        ok, reason = validate_url("http://internal.service/api")
        assert not ok

    # Edge cases
    def test_empty_url(self):
        ok, _ = validate_url("")
        assert not ok

    def test_no_hostname(self):
        ok, _ = validate_url("http:///path")
        assert not ok

    def test_0_0_0_0_blocked(self):
        ok, _ = validate_url("http://0.0.0.0/")
        assert not ok

    # Whitelist
    def test_whitelist_allows(self):
        ok, reason = validate_url(
            "http://100.64.1.1/", whitelist=["100.64.0.0/10"]
        )
        assert ok
        assert "whitelisted" in reason

    def test_whitelist_doesnt_affect_others(self):
        ok, _ = validate_url(
            "http://127.0.0.1/", whitelist=["100.64.0.0/10"]
        )
        assert not ok


class TestContainsInternalUrl:
    """Поиск внутренних URL в тексте команд."""

    def test_curl_to_localhost(self):
        findings = contains_internal_url("curl http://localhost:8080/api")
        assert len(findings) >= 1

    def test_wget_to_private(self):
        findings = contains_internal_url("wget http://192.168.1.1/config")
        assert len(findings) >= 1

    def test_curl_to_external(self):
        findings = contains_internal_url("curl https://api.github.com/repos")
        assert len(findings) == 0

    def test_no_urls(self):
        findings = contains_internal_url("ls -la /tmp/")
        assert len(findings) == 0

    def test_metadata_in_command(self):
        findings = contains_internal_url(
            "curl http://169.254.169.254/latest/meta-data/"
        )
        assert len(findings) >= 1

    def test_multiple_urls(self):
        findings = contains_internal_url(
            "curl http://localhost:3000 && curl https://example.com"
        )
        assert len(findings) == 1  # Только localhost


@pytest.mark.asyncio
class TestSsrfHook:
    """Тесты on_tool_use хука."""

    async def test_webfetch_external_passes(self):
        from src.hooks import HookContext

        hook_fn = make_ssrf_hook()
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={
                "tool_name": "WebFetch",
                "tool_input": {"url": "https://example.com/api"},
            },
        )
        result = await hook_fn(ctx)
        assert "ssrf_blocked" not in result.data

    async def test_webfetch_localhost_blocked(self):
        from src.hooks import HookContext

        hook_fn = make_ssrf_hook()
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={
                "tool_name": "WebFetch",
                "tool_input": {"url": "http://localhost:8080/admin"},
            },
        )
        result = await hook_fn(ctx)
        assert result.data.get("ssrf_blocked") is True

    async def test_webfetch_private_ip_blocked(self):
        from src.hooks import HookContext

        hook_fn = make_ssrf_hook()
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={
                "tool_name": "WebFetch",
                "tool_input": {"url": "http://10.0.0.5:3000/"},
            },
        )
        result = await hook_fn(ctx)
        assert result.data.get("ssrf_blocked") is True

    async def test_bash_with_internal_url_warning(self):
        from src.hooks import HookContext

        hook_fn = make_ssrf_hook()
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "curl http://192.168.1.1/config"},
            },
        )
        result = await hook_fn(ctx)
        assert "ssrf_warning" in result.data

    async def test_bash_external_no_warning(self):
        from src.hooks import HookContext

        hook_fn = make_ssrf_hook()
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "curl https://api.github.com/repos"},
            },
        )
        result = await hook_fn(ctx)
        assert "ssrf_warning" not in result.data

    async def test_other_tools_ignored(self):
        from src.hooks import HookContext

        hook_fn = make_ssrf_hook()
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={
                "tool_name": "Read",
                "tool_input": {"file_path": "/etc/passwd"},
            },
        )
        result = await hook_fn(ctx)
        assert "ssrf_blocked" not in result.data

    async def test_whitelist_in_hook(self):
        from src.hooks import HookContext

        hook_fn = make_ssrf_hook(whitelist=["100.64.0.0/10"])
        ctx = HookContext(
            event="on_tool_use",
            agent_name="test",
            data={
                "tool_name": "WebFetch",
                "tool_input": {"url": "http://100.64.1.1/api"},
            },
        )
        result = await hook_fn(ctx)
        assert "ssrf_blocked" not in result.data
