"""Unit tests for src.miniapp.setup_flow."""
from __future__ import annotations

import socket

import pytest

from src.miniapp import setup_flow


# ── validate_domain ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("example.com", "example.com"),
        ("Example.COM", "example.com"),
        ("example.com.", "example.com"),
        ("sub.bot.example.co.uk", "sub.bot.example.co.uk"),
        ("a-b.c-d.io", "a-b.c-d.io"),
    ],
)
def test_validate_domain_ok(raw, normalized):
    assert setup_flow.validate_domain(raw) == normalized


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "localhost",
        "-bad.example.com",
        "bad-.example.com",
        "bad..com",
        "a" * 260 + ".com",
        "under_score.example.com",
        "space in.com",
    ],
)
def test_validate_domain_rejects_bad(bad):
    with pytest.raises(ValueError):
        setup_flow.validate_domain(bad)


# ── validate_email ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "good", ["you@example.com", "ops+bot@sub.example.io", "a.b_c-d@x.co"]
)
def test_validate_email_ok(good):
    assert setup_flow.validate_email(good) == good


@pytest.mark.parametrize("bad", ["", "noatsign", "foo@", "@bar.com", "foo@bar"])
def test_validate_email_rejects_bad(bad):
    with pytest.raises(ValueError):
        setup_flow.validate_email(bad)


# ── pick_free_port ──────────────────────────────────────────────────

def test_pick_free_port_returns_port_in_range():
    port = setup_flow.pick_free_port(start=9100, end=9105)
    assert 9100 <= port <= 9105


def test_pick_free_port_skips_busy():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))  # ephemeral
        busy_port = busy.getsockname()[1]
        busy.listen(1)
        picked = setup_flow.pick_free_port(
            start=busy_port, end=busy_port + 3
        )
        assert picked != busy_port
        assert busy_port < picked <= busy_port + 3


def test_pick_free_port_raises_when_none_free():
    # Bind one socket, then ask for a window containing only that port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        p = busy.getsockname()[1]
        with pytest.raises(RuntimeError):
            setup_flow.pick_free_port(start=p, end=p)


# ── update_env_file ─────────────────────────────────────────────────

def test_update_env_replaces_existing_key_in_place(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# header comment\n"
        "FOO=old\n"
        "BAR=keep\n"
        "# trailing note\n",
        encoding="utf-8",
    )
    setup_flow.update_env_file(p, {"FOO": "new"})
    text = p.read_text(encoding="utf-8")
    assert "FOO=new" in text
    assert "FOO=old" not in text
    assert "BAR=keep" in text
    assert "# header comment" in text
    assert "# trailing note" in text
    # Key should stay at its original position (line 2).
    assert text.splitlines()[1] == "FOO=new"


def test_update_env_appends_missing_keys(tmp_path):
    p = tmp_path / ".env"
    p.write_text("EXISTING=1\n", encoding="utf-8")
    setup_flow.update_env_file(p, {"NEW_KEY": "x", "ANOTHER": "y"})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "EXISTING=1"
    assert "NEW_KEY=x" in lines
    assert "ANOTHER=y" in lines


def test_update_env_idempotent(tmp_path):
    p = tmp_path / ".env"
    p.write_text("HTTP_PORT=8089\n", encoding="utf-8")
    updates = {"HTTP_PORT": "8089", "PUBLIC_BASE_URL": "https://x.com"}
    setup_flow.update_env_file(p, updates)
    first = p.read_text(encoding="utf-8")
    setup_flow.update_env_file(p, updates)
    second = p.read_text(encoding="utf-8")
    assert first == second


def test_update_env_creates_file_if_missing(tmp_path):
    p = tmp_path / "new.env"
    setup_flow.update_env_file(p, {"A": "1"})
    assert p.read_text(encoding="utf-8").strip() == "A=1"


def test_update_env_preserves_unrelated_comments_and_order(tmp_path):
    p = tmp_path / ".env"
    src = (
        "# section A\n"
        "A=1\n"
        "\n"
        "# section B\n"
        "B=2\n"
        "C=3\n"
    )
    p.write_text(src, encoding="utf-8")
    setup_flow.update_env_file(p, {"B": "22"})
    out = p.read_text(encoding="utf-8").splitlines()
    assert out == ["# section A", "A=1", "", "# section B", "B=22", "C=3"]
