"""Rate limit on the public sign-in addresses of single sign-on (/auth/login, /auth/callback)."""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from core import api, auth_throttle

IDP = "https://idp.example/authorize?x=1"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTH_SSO_MAX_PER_MINUTE", "3")
    clock = {"t": 1000.0}
    monkeypatch.setattr(auth_throttle, "_clock", lambda: clock["t"])
    monkeypatch.setattr(api.core_sso, "enabled", lambda: True)
    monkeypatch.setattr(api.core_sso, "start_login", lambda: IDP)

    def refuse(state, code, error):
        raise api.core_oidc.OidcError("Sign-in refused", "test")
    monkeypatch.setattr(api.core_sso, "finish_login", refuse)
    c = TestClient(api.app)
    c.clock = clock
    return c


def login(c, **headers):
    return c.get("/auth/login", follow_redirects=False, headers=headers)


def callback(c):
    return c.get("/auth/callback?code=x&state=y", follow_redirects=False)


def test_login_is_limited_per_client_and_a_spoofed_header_does_not_help(client):
    first = login(client)
    assert first.status_code == 302 and first.headers["location"] == IDP
    assert [login(client).status_code for _ in range(2)] == [302, 302]
    spoof = login(client, **{"x-forwarded-for": "9.9.9.9"})
    assert spoof.status_code == 429
    wait = int(spoof.headers["retry-after"])
    assert 1 <= wait <= 60
    assert "Too many sign-in attempts" in spoof.json()["detail"]


def test_the_budget_comes_back_and_refused_requests_do_not_extend_the_wait(client):
    assert [login(client).status_code for _ in range(3)] == [302, 302, 302]
    client.clock["t"] += 30
    assert login(client).status_code == 429
    client.clock["t"] += 31          # 61 s after the first three; the refused call at +30 was not counted
    assert login(client).status_code == 302


def test_the_callback_sends_the_person_back_with_a_message(client, monkeypatch):
    monkeypatch.setenv("AUTH_SSO_MAX_PER_MINUTE", "2")
    first, second, third = callback(client), callback(client), callback(client)
    assert "Sign-in%20refused" in first.headers["location"] and "Sign-in%20refused" in second.headers["location"]
    assert third.status_code == 302 and "Too%20many%20sign-in%20attempts" in third.headers["location"]


def test_login_and_callback_have_separate_budgets(client, monkeypatch):
    monkeypatch.setenv("AUTH_SSO_MAX_PER_MINUTE", "2")
    assert [login(client).status_code for _ in range(3)] == [302, 302, 429]
    assert "Sign-in%20refused" in callback(client).headers["location"]


def test_a_switched_off_sso_does_not_use_the_budget(client, monkeypatch):
    monkeypatch.setenv("AUTH_SSO_MAX_PER_MINUTE", "2")
    monkeypatch.setattr(api.core_sso, "enabled", lambda: False)
    assert [login(client).status_code for _ in range(5)] == [404] * 5
    monkeypatch.setattr(api.core_sso, "enabled", lambda: True)
    assert login(client).status_code == 302


def test_the_switch_and_the_setting(client, monkeypatch):
    monkeypatch.setenv("AUTH_SSO_MAX_PER_MINUTE", "2")
    monkeypatch.setenv("AUTH_THROTTLE", "off")
    assert [login(client).status_code for _ in range(6)] == [302] * 6
    for raw, want in (("", 30), ("abc", 30), ("5", 5), ("999999", 10000)):
        monkeypatch.setenv("AUTH_SSO_MAX_PER_MINUTE", raw)
        assert auth_throttle.sso_limit() == want


def test_one_audit_line_per_client_and_minute(client, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="ragleap.audit")
    monkeypatch.setenv("AUTH_SSO_MAX_PER_MINUTE", "1")
    assert login(client).status_code == 302
    assert [login(client).status_code for _ in range(5)] == [429] * 5

    def lines():
        return [json.loads(r.getMessage().split("AUDIT ", 1)[1]) for r in caplog.records if r.name == "ragleap.audit"]
    got = [x for x in lines() if x["audit"] == "sso_rate_limited"]
    assert len(got) == 1 and got[0]["route"] == "auth_login" and got[0]["limit"] == 1 and got[0]["retry_after"] >= 1
    client.clock["t"] += 61
    assert login(client).status_code == 302
    assert login(client).status_code == 429
    assert len([x for x in lines() if x["audit"] == "sso_rate_limited"]) == 2


def test_the_number_of_tracked_clients_is_bounded(monkeypatch):
    monkeypatch.setattr(auth_throttle, "MAX_TRACKED_CLIENTS", 5)
    for i in range(30):
        auth_throttle.rate_limited("auth_login", "10.0.0.%d" % i)
    assert len(auth_throttle._rate) <= 5
