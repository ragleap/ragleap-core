"""People and personal keys (core/users.py), the roles in the API middleware, and the owner-only user routes.
Uses the real database (db/schema.sql applied); every person made here is named pytest-... and removed afterwards."""
import uuid

import pytest
from fastapi.testclient import TestClient

from core import document_store, folder_ingest, users

BOOT = {"x-api-key": "boot-key"}


def _sql(sql, args=(), fetch=False):
    conn = document_store._connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _name():
    return "pytest-" + uuid.uuid4().hex[:8]


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
    return TestClient(api.app)


def person(role, label="t"):
    u = users.create_user(_name(), role)
    k = users.create_key(u["id"], label)
    return u, k["key"], k


# ---------------- keys ----------------

def test_a_key_is_shown_once_and_only_its_hash_is_stored():
    u, key, made = person("operator")
    assert key.startswith("rlk_") and len(key) > 40 and made["key_prefix"] == key[:8]
    rows = _sql("SELECT user_keys::text FROM user_keys WHERE user_id = %s", (u["id"],), True)
    assert len(rows) == 1 and key not in rows[0][0] and users.hash_key(key) in rows[0][0]
    assert key not in str(users.list_users())


def test_resolve_finds_the_person_and_ignores_everything_else():
    u, key, _ = person("admin")
    got = users.resolve(key)
    assert got["name"] == u["name"] and got["role"] == "admin" and got["user_id"] == u["id"] and got["kind"] == "user"
    for bad in ("", None, 5, "rlk_nope", "boot-key", "rlk_" + "x" * 200, key + "x", key[:-1]):
        assert users.resolve(bad) is None


def test_keys_that_do_not_look_like_ours_never_touch_the_database(monkeypatch):
    monkeypatch.setattr(users, "_lookup", lambda h: (_ for _ in ()).throw(AssertionError("database was asked")))
    assert users.resolve("some-other-key") is None and users.resolve("rlk_" + "x" * 200) is None


def test_a_revoked_key_stops_working_at_once():
    u, key, made = person("viewer")
    assert users.resolve(key)
    users.revoke_key(u["id"], made["key_id"])
    assert users.resolve(key) is None
    with pytest.raises(users.UserError) as e:
        users.revoke_key(u["id"], made["key_id"])
    assert e.value.status == 404


def test_a_disabled_person_cannot_sign_in_and_gets_no_new_key():
    u, key, _ = person("operator")
    users.update_user(u["id"], status="disabled")
    assert users.resolve(key) is None
    with pytest.raises(users.UserError) as e:
        users.create_key(u["id"])
    assert e.value.status == 409
    users.update_user(u["id"], status="active")
    assert users.resolve(key)["name"] == u["name"]


def test_a_role_change_takes_effect_at_once():
    u, key, _ = person("viewer")
    assert users.resolve(key)["role"] == "viewer"
    users.update_user(u["id"], role="admin")
    assert users.resolve(key)["role"] == "admin"


def test_a_found_key_is_remembered_for_a_short_time(monkeypatch):
    u, key, _ = person("viewer")
    users.resolve(key)
    monkeypatch.setattr(users, "_lookup", lambda h: (_ for _ in ()).throw(AssertionError("asked again")))
    assert users.resolve(key)["name"] == u["name"]


def test_last_use_is_recorded():
    u, key, made = person("viewer")
    assert _sql("SELECT last_used_at FROM user_keys WHERE id = %s", (made["key_id"],), True)[0][0] is None
    users.resolve(key)
    assert _sql("SELECT last_used_at FROM user_keys WHERE id = %s", (made["key_id"],), True)[0][0] is not None


def test_a_database_problem_means_unknown_key_not_an_error(monkeypatch):
    monkeypatch.setattr(users, "_lookup", lambda h: (_ for _ in ()).throw(RuntimeError("db down")))
    assert users.resolve("rlk_" + "a" * 43) is None


# ---------------- making people ----------------

@pytest.mark.parametrize("name,role", [("", "viewer"), ("   ", "viewer"), ("x" * 81, "viewer"), ("bad\nname", "viewer"),
                                       (None, "viewer"), ("pytest-ok", "root"), ("pytest-ok", None)])
def test_bad_input_is_refused(name, role):
    with pytest.raises(users.UserError) as e:
        users.create_user(name, role)
    assert e.value.status == 400


def test_two_people_cannot_share_a_name_even_in_another_case():
    n = _name()
    users.create_user(n, "viewer")
    with pytest.raises(users.UserError) as e:
        users.create_user(n.upper(), "admin")
    assert e.value.status == 409


def test_ids_must_be_ids():
    for call in (lambda: users.update_user("nope", role="viewer"), lambda: users.create_key("nope"),
                 lambda: users.revoke_key("nope", "nope")):
        with pytest.raises(users.UserError) as e:
            call()
        assert e.value.status == 404
    with pytest.raises(users.UserError) as e:
        users.update_user(str(uuid.uuid4()), role="viewer")
    assert e.value.status == 404
    with pytest.raises(users.UserError):
        users.update_user(person("viewer")[0]["id"])


# ---------------- the API ----------------

def test_the_shared_key_is_the_owner_and_the_open_api_is_open(client, monkeypatch):
    me = client.get("/me", headers=BOOT).json()
    assert me == {"kind": "bootstrap", "name": "Owner (shared key)", "role": "owner", "user_id": None}
    assert client.get("/me").status_code == 401 and client.get("/users").status_code == 401
    from core import api
    monkeypatch.setattr(api, "RAGLEAP_API_KEY", "")
    assert client.get("/me").json()["kind"] == "open" and client.get("/users").status_code == 200


def test_a_personal_key_shows_its_person_in_me(client):
    u, key, _ = person("operator")
    assert client.get("/me", headers={"x-api-key": key}).json() == {
        "kind": "user", "name": u["name"], "role": "operator", "user_id": u["id"]}


def test_the_owner_makes_people_and_keys_through_the_api(client):
    n = _name()
    made = client.post("/users", json={"name": n, "role": "operator"}, headers=BOOT)
    assert made.status_code == 200 and made.json()["role"] == "operator"
    uid = made.json()["id"]
    k = client.post("/users/%s/keys" % uid, json={"label": "laptop"}, headers=BOOT).json()
    assert k["key"].startswith("rlk_") and "only once" in k["note"]
    listing = client.get("/users", headers=BOOT).json()
    mine = [x for x in listing["users"] if x["id"] == uid][0]
    assert mine["active_keys"] == 1 and k["key"] not in str(listing) and listing["roles"] == list(users.ROLES)
    assert [(x["id"], x["prefix"], x["label"]) for x in mine["keys"]] == [(k["key_id"], k["key_prefix"], "laptop")]
    assert set(mine["keys"][0]) == {"id", "prefix", "label", "created_at", "last_used_at"}      # never the key or its hash
    assert client.patch("/users/%s" % uid, json={"role": "viewer"}, headers=BOOT).json()["role"] == "viewer"
    assert client.get("/me", headers={"x-api-key": k["key"]}).json()["role"] == "viewer"
    assert client.delete("/users/%s/keys/%s" % (uid, k["key_id"]), headers=BOOT).status_code == 200
    assert client.get("/me", headers={"x-api-key": k["key"]}).status_code == 401


def test_the_user_routes_pass_refusals_through(client):
    assert client.post("/users", json={"name": "", "role": "viewer"}, headers=BOOT).status_code == 400
    assert client.post("/users", json={"name": "pytest-x", "role": "root"}, headers=BOOT).status_code == 400
    assert client.patch("/users/not-an-id", json={"role": "viewer"}, headers=BOOT).status_code == 404
    assert client.post("/users", json={"name": "pytest-x"}, headers=BOOT).status_code == 422


def test_each_role_gets_exactly_its_doors(client, monkeypatch):
    monkeypatch.setattr(folder_ingest, "approve_grant", lambda g: (200, {"approved": g, "files": 1}))
    keys = {r: {"x-api-key": person(r)[1]} for r in users.ROLES}
    tries = [  # (method, path, lowest role that may; the body is only for routes that need one)
        ("GET", "/me", "viewer", None), ("GET", "/folder-ingest", "viewer", None),
        ("GET", "/autonomy/pending", "operator", None),
        ("POST", "/folder-ingest/approve", "admin", {"grant": "docs"}),
        ("GET", "/users", "owner", None),
    ]
    for method, path, lowest, body in tries:
        for role in users.ROLES:
            r = client.request(method, path, json=body, headers=keys[role])
            allowed = users.ROLES.index(role) >= users.ROLES.index(lowest)
            assert (r.status_code != 403) is allowed, (method, path, role, r.status_code)
            if not allowed:
                assert "needs %s or higher" % lowest in r.json()["detail"] and ("Your role (%s)" % role) in r.json()["detail"]


def test_a_viewer_cannot_upload_or_change_settings_or_manage_people(client):
    h = {"x-api-key": person("viewer")[1]}
    assert client.post("/upload", headers=h).status_code == 403
    assert client.put("/settings", json={}, headers=h).status_code == 403
    assert client.post("/users", json={"name": "pytest-z", "role": "owner"}, headers=h).status_code == 403
    assert client.delete("/documents/%s" % uuid.uuid4(), headers=h).status_code == 403


def test_an_operator_can_decide_on_actions_but_not_change_settings(client):
    h = {"x-api-key": person("operator")[1]}
    assert client.post("/autonomy/pending/%s/approve" % uuid.uuid4(), headers=h).status_code != 403
    assert client.put("/settings", json={}, headers=h).status_code == 403


def test_a_wrong_personal_key_is_refused_like_any_wrong_key(client):
    assert client.get("/me", headers={"x-api-key": "rlk_" + "z" * 43}).status_code == 401
    u, key, _ = person("viewer")
    users.update_user(u["id"], status="disabled")
    assert client.get("/me", headers={"x-api-key": key}).status_code == 401


def test_public_routes_still_need_no_key(client):
    assert client.get("/health").status_code == 200 and client.get("/office").status_code == 200
