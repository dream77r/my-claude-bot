import json

import pytest

from src.iiko_grant_term import valid_term
from tests.test_iiko_grant_limit import env, write


def test_consent_and_revocation(env):
    gid, original, path, call = env
    text, button = call(gid + " until_revoked")
    assert "до отзыва" in text.lower() and "12100.00" in text and "east, west" in text
    assert json.loads(path.read_text()) == original
    assert valid_term(original, 8999) and not valid_term(original, 9000)
    call(button.split(" ", 1)[1])
    live = json.loads(path.read_text())
    assert live == dict(original, expires=None, expiryMode="until_revoked",
                        termConfirmedBy=17, termConfirmedAt=1000)
    assert valid_term(live, 9999999999)
    assert not valid_term(dict(live, revoked=True), 1001)
    with pytest.raises(ValueError):
        call(button.split(" ", 1)[1])
    assert call(gid + " until_revoked", now=9999999999)[1] is None
    _, button = call(gid + " 50000", now=10001)
    call(button.split(" ", 1)[1], now=10002)
    assert json.loads(path.read_text()) == dict(live, ceiling="50000.00")


@pytest.mark.parametrize("bad", [{}, {"expires": None}, {"expiryMode": "until_revoked"},
    {"expiryMode": "until_revoked", "expires": None}, {"expires": True},
    {"expires": float("inf")}, {"expiryMode": "typo", "expires": 9000},
    {"expiryMode": "until_revoked", "expires": None, "termConfirmedBy": 18, "termConfirmedAt": 7},
    {"expiryMode": "until_revoked", "expires": None, "termConfirmedBy": 17, "termConfirmedAt": 9001}])
def test_missing_or_malformed_expiry_is_denied(bad):
    assert not valid_term(dict(owner=17, **bad), 1000)


@pytest.mark.parametrize("change", [{"revoked": True}, {"owner": 18}, {"ceiling": "50000.00"},
                                    {"organizations": ["east"]}, {"expires": 8000}])
def test_explicit_term_confirmation_is_bound_to_grant(env, change):
    gid, grant, path, call = env
    _, key = call(gid + " until_revoked")
    write(path, dict(grant, **change))
    with pytest.raises(ValueError):
        call(key.split(" ", 1)[1])
    assert json.loads(path.read_text()) == dict(grant, **change)


def test_expired_grant_or_proposal_cannot_be_extended(env):
    gid, grant, path, call = env
    with pytest.raises(ValueError):
        call(gid + " until_revoked", now=9001)
    _, key = call(gid + " until_revoked", now=8990)
    with pytest.raises(ValueError):
        call(key.split(" ", 1)[1], now=9000)
    assert json.loads(path.read_text()) == grant


def test_limit_button_cannot_confirm_term(env):
    gid, grant, path, call = env
    _, key = call(gid + " until_revoked")
    with pytest.raises(ValueError):
        call(key.split(" ", 1)[1].replace("confirm-term", "confirm"))
    assert json.loads(path.read_text()) == grant


def test_real_store_checks_term_scope_owner_and_token(tmp_path, monkeypatch):
    import hashlib
    import secrets
    from uuid import uuid4
    from src.iiko_bb_grants import AccessDenied, GrantStore, ReviewAccess

    gid, key = str(uuid4()), secrets.token_urlsafe(32)
    store = GrantStore(tmp_path)
    grant = dict(id=gid, project="proj_test", owner=17, organizations=["shop"], ceiling="50000.00",
                 digest=hashlib.sha256(key.encode()).hexdigest(), expires=None, expiryMode="until_revoked",
                 termConfirmedBy=17, termConfirmedAt=1000)
    path = tmp_path / (gid + ".json")
    write(path, grant)
    token = "bb_iiko." + gid + "." + key
    access = ReviewAccess(store, token, "proj_test", lambda uid: uid == 17, {"shop"})
    monkeypatch.setattr("src.iiko_bb_grants.time.time", lambda: 10000000000)
    assert access.grant() == grant
    with pytest.raises(AccessDenied):
        store.active("bb_iiko." + gid + "." + secrets.token_urlsafe(32))
    with pytest.raises(AccessDenied):
        ReviewAccess(store, token, "proj_other", lambda uid: True, {"shop"}).grant()
    with pytest.raises(AccessDenied):
        ReviewAccess(store, token, "proj_test", lambda uid: False, {"shop"}).grant()
    for org, amount in [("other", 1), ("shop", 50000.01)]:
        with pytest.raises(AccessDenied):
            access.inspect({"organizationId": org, "document": {"items": [{"sum": amount}]}})
    write(path, dict(grant, revoked=True))
    with pytest.raises(AccessDenied):
        access.grant()
