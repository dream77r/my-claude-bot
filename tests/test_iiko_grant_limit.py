import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as Obj
from uuid import uuid4

import pytest

from src.iiko_grant_limit import change_limit, private_owner


def write(path, data):
    path.write_text(json.dumps(data))


@pytest.fixture
def env(tmp_path):
    gid = str(uuid4())
    grant = dict(id=gid, project="proj_test", owner=17, digest="test-only",
                 organizations=["east", "west"], ceiling="12100.00", expires=9000)
    path = tmp_path / (gid + ".json")
    write(path, grant)
    lock = threading.Lock()
    w = Obj(locked=lambda: lock, client=Obj(allowed={"east", "west"}),
            catalog=lambda: {"organizations": [{"id": x, "name": x} for x in ["east", "west"]]})
    def call(args, owner=17, now=1000):
        return change_limit(w, tmp_path, write, owner, args.split(), now=lambda: now)
    return gid, grant, path, call


def test_confirm_preserves_authority_and_replay_fails(env):
    gid, grant, path, call = env
    text, keyboard = call(gid + " 50000")
    assert "12100.00" in text and "50000.00" in text and "east" in text and "UTC" in text
    assert json.loads(path.read_text()) == grant
    call(keyboard.split(" ", 1)[1])
    assert json.loads(path.read_text()) == dict(grant, ceiling="50000.00")
    with pytest.raises(ValueError):
        call(keyboard.split(" ", 1)[1])


@pytest.mark.parametrize("amount", ["-10", "0", "0.001", "NaN", "Infinity", "10000000000", "1e9"])
def test_invalid_amount(env, amount):
    gid, grant, path, call = env
    with pytest.raises(ValueError):
        call(gid + " " + amount)
    assert json.loads(path.read_text()) == grant


@pytest.mark.parametrize("changed", [{"revoked": True}, {"expires": 999}, {"ceiling": "200.00"},
                                     {"owner": 18}, {"organizations": ["east"]}, {"project": "proj_other"},
                                     {"digest": "replaced"}])
def test_stale_confirm(env, changed):
    gid, grant, path, call = env
    _, key = call(gid + " 50000")
    write(path, dict(grant, **changed))
    with pytest.raises(ValueError):
        call(key.split(" ", 1)[1])
    assert json.loads(path.read_text()) == dict(grant, **changed)


def test_expiry_replacement_and_owner(env):
    gid, grant, path, call = env
    with pytest.raises(ValueError):
        call(gid + " 50000", owner=18)
    _, old = call(gid + " 55000")
    _, new = call(gid + " 50000")
    for command, owner, now in [(old, 17, 1001), (new, 18, 1001), (new, 17, 1600)]:
        with pytest.raises(ValueError):
            call(command.split(" ", 1)[1], owner=owner, now=now)
    assert json.loads(path.read_text()) == grant


def test_confirm_concurrently(env):
    gid, _, path, call = env
    _, key = call(gid + " 50000")
    def apply(_):
        try:
            call(key.split(" ", 1)[1])
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(2) as workers:
        assert sorted(workers.map(apply, range(2))) == [False, True]
    assert json.loads(path.read_text())["ceiling"] == "50000.00"


@pytest.mark.parametrize("case", ["valid", "edited", "forward", "via_bot", "wrong_user",
                                  "bot", "group", "wrong_chat", "sender_chat", "not_allowed"])
def test_sender(case):
    user = Obj(id=17, is_bot=(case == "bot"))
    message = Obj(from_user=Obj(id=18 if case == "wrong_user" else 17),
                  forward_origin=case == "forward", via_bot=case == "via_bot",
                  sender_chat=case == "sender_chat")
    update = Obj(message=None if case == "edited" else message, effective_user=user,
                 effective_chat=Obj(type="group" if case == "group" else "private",
                                    id=18 if case == "wrong_chat" else 17))
    assert private_owner(update, 17, lambda uid: case != "not_allowed") == (case == "valid")


@pytest.mark.parametrize("agent_name,enabled,registered", [
    ("iiko_invoice", "1", True), ("other", "1", False), ("iiko_invoice", "0", False)])
def test_router(monkeypatch, agent_name, enabled, registered):
    from src.telegram_bridge import TelegramBridge
    from src.iiko_grant_limit import handle_limit
    monkeypatch.setenv("IIKO_BB_ENABLED", enabled)
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.agent = Obj(name=agent_name)
    route = bridge._build_router().route("/iiko_limit id 50000")
    if registered:
        assert route.handler.func is handle_limit
        assert route.args == "id 50000"
    else:
        assert route is None


def test_noop_cancels_pending(env):
    gid, grant, path, call = env
    _, key = call(gid + " 50000")
    assert call(gid + " 12100")[1] is None
    with pytest.raises(ValueError):
        call(key.split(" ", 1)[1])
    assert json.loads(path.read_text()) == grant
