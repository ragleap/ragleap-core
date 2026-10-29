"""SHARED_KEY_DISABLED: refuse the shared RAGLEAP_API_KEY once an active owner has a personal key.
Uses the real database; every person made here is named pytest-... and removed afterwards."""
import uuid

import pytest
from fastapi.testclient import TestClient

from core import document_store, users

BOOT = {"x-api-key": "boot-key"}


def _sql(sql, args=()):
    conn = document_store._connect()
    try:
        conn.cursor().execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _person(role):
    u = users.create_user("pytest-" + uuid.uuid4().hex[:8], role)
    return u, users.create_key(u["id"], "t")


@pytest.fixture(autouse=True)
def clean():
    users._clear_cache()
    yield
    _sql("DELETE FROM users WHERE name LIKE %s", ("pytest-%",))
    users._clear_cache()


@pytest.fixture
def client(monkeypatch):
    from core import api
    monkeypatch.setattr(api, "RAGLEAP_API_KEY", "boot-key")
    monkeypatch.setattr(api, "_shared_key_warned", [])
    return TestClient(api.app)


def _switch(monkeypatch, on):
    from core import api
    monkeypatch.setattr(api, "SHARED_KEY_DISABLED", on)


def test_the_switch_is_off_by_default_and_the_shared_key_keeps_working(client, monkeypatch):
    from core import api
    assert api.SHARED_KEY_DISABLED is False
    _person("owner")
    users._clear_cache()
    r = client.get("/me", headers=BOOT)
    assert r.status_code == 200 and r.json()["kind"] == "bootstrap"


def test_with_the_switch_on_and_a_live_owner_the_shared_key_is_refused(client, monkeypatch):
    _switch(monkeypatch, True)
    _, made = _person("owner")
    users._clear_cache()
    r = client.get("/me", headers=BOOT)
    assert r.status_code == 401 and "personal key" in r.json()["detail"]
    assert "boot-key" not in r.text
    ok = client.get("/me", headers={"x-api-key": made["key"]})
    assert ok.status_code == 200 and ok.json()["role"] == "owner"


def test_with_no_live_owner_the_shared_key_still_works_so_nobody_is_locked_out(client, monkeypatch):
    _switch(monkeypatch, True)
    users._clear_cache()
    monkeypatch.setattr(users, "has_live_owner", lambda: False)
    assert client.get("/me", headers=BOOT).status_code == 200


def test_a_wrong_key_is_still_refused_with_the_switch_on(client, monkeypatch):
    _switch(monkeypatch, True)
    assert client.get("/me", headers={"x-api-key": "nope"}).status_code == 401


def test_only_an_active_owner_with_a_live_key_counts():
    users._clear_cache()
    base = users.has_live_owner()
    viewer, _ = _person("viewer")
    users._clear_cache()
    assert users.has_live_owner() is base          # a viewer's key never counts
    owner, made = _person("owner")
    users._clear_cache()
    assert users.has_live_owner() is True
    users.revoke_key(owner["id"], made["key_id"])
    users._clear_cache()
    assert users.has_live_owner() is base          # revoked key: back to what it was
    k2 = users.create_key(owner["id"], "again")
    users._clear_cache()
    assert users.has_live_owner() is True
    users.update_user(owner["id"], status="disabled")
    assert users.has_live_owner() is base          # disabled person: does not count
    assert k2["key"] not in str(users.list_users())


def test_a_database_problem_means_no_owner_so_the_shared_key_keeps_working(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    users._clear_cache()
    monkeypatch.setattr(users, "_connect", boom)
    assert users.has_live_owner() is False


def test_revoking_the_last_owner_key_gives_the_shared_key_back_at_once(client, monkeypatch):
    _switch(monkeypatch, True)
    owner, made = _person("owner")
    users._clear_cache()
    if client.get("/me", headers=BOOT).status_code != 401:
        pytest.skip("another owner already exists in this database")
    users.revoke_key(owner["id"], made["key_id"])
    if users.has_live_owner():
        pytest.skip("another owner already exists in this database")
    assert client.get("/me", headers=BOOT).status_code == 200
