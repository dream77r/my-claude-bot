"""Тесты для src/miniapp/auth.py — валидация Telegram WebApp initData."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from src.http_server import create_app
from src.miniapp.auth import (
    AuthError,
    accessible_agents,
    extract_user_id,
    parse_init_data,
    validate_init_data,
)


def make_init_data(
    bot_token: str,
    user_id: int = 1001,
    auth_date: int | None = None,
    extra: dict | None = None,
    corrupt_hash: bool = False,
) -> str:
    """Собрать валидный (или намеренно испорченный) initData."""
    fields: dict[str, str] = {
        "auth_date": str(auth_date or int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Test"}),
        "query_id": "AAHdF6IQAAAAAN0XohAh",
    }
    if extra:
        fields.update(extra)

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if corrupt_hash:
        digest = "0" * len(digest)
    fields["hash"] = digest
    return urlencode(fields)


class FakeAgent:
    def __init__(self, name: str, token: str, allowed: list[int], master: bool = False):
        self.name = name
        self.display_name = name.title()
        self.role = "test"
        self.bot_token = token
        self.allowed_users = allowed
        self.is_master = master


class FakeRuntime:
    def __init__(self, agents: dict):
        self.agents = agents

    def running_agents(self) -> list[str]:
        return list(self.agents)


# ── Pure validation ────────────────────────────────────────────────────────


class TestValidateInitData:
    TOKEN = "123456:ABCDEF_test_token"

    def test_valid_init_data_passes(self):
        raw = make_init_data(self.TOKEN, user_id=42)
        fields = validate_init_data(raw, self.TOKEN)
        assert "auth_date" in fields
        assert extract_user_id(fields) == 42

    def test_corrupt_hash_rejected(self):
        raw = make_init_data(self.TOKEN, corrupt_hash=True)
        with pytest.raises(AuthError, match="hash mismatch"):
            validate_init_data(raw, self.TOKEN)

    def test_missing_hash_rejected(self):
        raw = urlencode({"auth_date": str(int(time.time())), "user": "{}"})
        with pytest.raises(AuthError, match="hash missing"):
            validate_init_data(raw, self.TOKEN)

    def test_wrong_token_rejected(self):
        raw = make_init_data(self.TOKEN)
        with pytest.raises(AuthError, match="hash mismatch"):
            validate_init_data(raw, "different_token")

    def test_expired_auth_date_rejected(self):
        old = int(time.time()) - 7200
        raw = make_init_data(self.TOKEN, auth_date=old)
        with pytest.raises(AuthError, match="expired"):
            validate_init_data(raw, self.TOKEN, max_age=3600)

    def test_future_auth_date_rejected(self):
        future = int(time.time()) + 600
        raw = make_init_data(self.TOKEN, auth_date=future)
        with pytest.raises(AuthError, match="future"):
            validate_init_data(raw, self.TOKEN)

    def test_empty_raw_rejected(self):
        with pytest.raises(AuthError, match="empty initData"):
            validate_init_data("", self.TOKEN)

    def test_empty_token_rejected(self):
        with pytest.raises(AuthError, match="empty bot_token"):
            validate_init_data("hash=x", "")

    def test_parse_init_data_roundtrip(self):
        raw = make_init_data(self.TOKEN, user_id=7)
        parsed = parse_init_data(raw)
        assert "hash" in parsed
        assert "user" in parsed

    def test_extract_user_id_missing(self):
        with pytest.raises(AuthError, match="user field missing"):
            extract_user_id({"auth_date": "1"})

    def test_extract_user_id_not_json(self):
        with pytest.raises(AuthError, match="not JSON"):
            extract_user_id({"user": "not-json"})

    def test_extract_user_id_not_int(self):
        with pytest.raises(AuthError, match="user.id not int"):
            extract_user_id({"user": json.dumps({"id": "abc"})})


# ── HTTP integration via TestClient ────────────────────────────────────────


class TestGetCurrentUserEndpoint:
    TOKEN_ME = "111:ME_TOKEN"
    TOKEN_CODER = "222:CODER_TOKEN"

    def _client(self, monkeypatch=None, founder: int | None = None):
        agents = {
            "me": FakeAgent("me", self.TOKEN_ME, allowed=[1001], master=True),
            "coder": FakeAgent("coder", self.TOKEN_CODER, allowed=[1002]),
        }
        runtime = FakeRuntime(agents)
        app = create_app(runtime)
        if monkeypatch is not None:
            if founder is not None:
                monkeypatch.setenv("FOUNDER_TELEGRAM_ID", str(founder))
            else:
                monkeypatch.delenv("FOUNDER_TELEGRAM_ID", raising=False)
        return TestClient(app), runtime

    def test_missing_authorization_header_401(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        r = client.get("/api/me", headers={"X-Origin-Agent": "me"})
        assert r.status_code == 401

    def test_missing_origin_agent_400(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        raw = make_init_data(self.TOKEN_ME, user_id=1001)
        r = client.get(
            "/api/me", headers={"Authorization": f"tma {raw}"}
        )
        assert r.status_code == 400

    def test_unknown_origin_agent_404(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        raw = make_init_data(self.TOKEN_ME, user_id=1001)
        r = client.get(
            "/api/me",
            headers={
                "Authorization": f"tma {raw}",
                "X-Origin-Agent": "ghost",
            },
        )
        assert r.status_code == 404

    def test_valid_initdata_allowed_user_ok(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        raw = make_init_data(self.TOKEN_ME, user_id=1001)
        r = client.get(
            "/api/me",
            headers={
                "Authorization": f"tma {raw}",
                "X-Origin-Agent": "me",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user_id"] == 1001
        assert body["origin_agent"] == "me"
        assert body["accessible_agents"] == ["me"]
        assert body["is_founder"] is False

    def test_founder_sees_all_agents(self, monkeypatch):
        client, _ = self._client(monkeypatch, founder=9999)
        raw = make_init_data(self.TOKEN_ME, user_id=9999)
        r = client.get(
            "/api/me",
            headers={
                "Authorization": f"tma {raw}",
                "X-Origin-Agent": "me",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["is_founder"] is True
        assert set(body["accessible_agents"]) == {"me", "coder"}

    def test_unknown_user_forbidden(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        raw = make_init_data(self.TOKEN_ME, user_id=7777)
        r = client.get(
            "/api/me",
            headers={
                "Authorization": f"tma {raw}",
                "X-Origin-Agent": "me",
            },
        )
        assert r.status_code == 403

    def test_hash_signed_by_wrong_agent_rejected(self, monkeypatch):
        """initData подписан токеном coder, но origin_agent=me → 401."""
        client, _ = self._client(monkeypatch)
        raw = make_init_data(self.TOKEN_CODER, user_id=1002)
        r = client.get(
            "/api/me",
            headers={
                "Authorization": f"tma {raw}",
                "X-Origin-Agent": "me",
            },
        )
        assert r.status_code == 401

    def test_origin_agent_via_query(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        raw = make_init_data(self.TOKEN_ME, user_id=1001)
        r = client.get(
            "/api/me?origin_agent=me",
            headers={"Authorization": f"tma {raw}"},
        )
        assert r.status_code == 200


class TestAccessibleAgentsHelper:
    def test_allowed_user_sees_their_agent(self):
        agents = {
            "me": FakeAgent("me", "x", [1001], master=True),
            "coder": FakeAgent("coder", "y", [1002]),
        }
        runtime = FakeRuntime(agents)
        assert accessible_agents(runtime, 1001) == ["me"]
        assert accessible_agents(runtime, 1002) == ["coder"]
        assert accessible_agents(runtime, 9999) == []

    def test_founder_sees_all(self, monkeypatch):
        monkeypatch.setenv("FOUNDER_TELEGRAM_ID", "777")
        agents = {
            "me": FakeAgent("me", "x", [1001], master=True),
            "coder": FakeAgent("coder", "y", [1002]),
        }
        runtime = FakeRuntime(agents)
        assert set(accessible_agents(runtime, 777)) == {"me", "coder"}
