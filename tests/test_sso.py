"""Single sign-on end to end: the real database and routes, with a fake OpenID Connect provider (its own signing key, served
through httpx's mock transport). Every person made here is named pytest-... and removed afterwards."""
import hashlib
import json
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from core import document_store, oidc, sso, users

ISSUER = "https://idp.example.test"
CLIENT = "ragleap-client"
BOOT = {"x-api-key": "boot-key"}
MAIL = "@sso.test"


def _sql(sql, args=()):
    conn = document_store._connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows
    finally:
        conn.close()


class Provider:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.id_token = ""

    def handler(self, request):
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                                             "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/keys"})
        if url == ISSUER + "/keys":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            jwk.update(kid="k1", use="sig", alg="RS256")
            return httpx.Response(200, json={"keys": [jwk]})
        if url == ISSUER + "/token":
            return httpx.Response(200, json={"id_token": self.id_token})
        return httpx.Response(404)

    def make(self, nonce_value, subject, email, **claims):
        now = int(time.time())
        body = {"iss": ISSUER, "aud": CLIENT, "sub": subject, "iat": now, "exp": now + 300, "nonce": nonce_value, "email": email,
                "email_verified": True, "amr": ["pwd", "otp"]}
        body.update(claims)
        return jwt.encode({k: v for k, v in body.items() if v is not None}, self.key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def env(monkeypatch):
    from core import api
    oidc.reset_cache()
    provider = Provider()
    monkeypatch.setattr(oidc, "_make_client", lambda: httpx.Client(transport=httpx.MockTransport(provider.handler), follow_redirects=False))
    monkeypatch.setattr(api, "RAGLEAP_API_KEY", "boot-key")
    for k, v in (("OIDC_ISSUER", ISSUER), ("OIDC_CLIENT_ID", CLIENT), ("OIDC_CLIENT_SECRET", "x" * 12),
                 ("OIDC_REDIRECT_URL", "https://ragleap.example.test/auth/callback")):
        monkeypatch.setenv(k, v)
    sso._warned.clear()
    yield {"client": TestClient(api.app), "idp": provider}
    _sql("DELETE FROM users WHERE name LIKE %s", ("pytest-%",))
    _sql("DELETE FROM sso_logins")
    _sql("DELETE FROM audit_events WHERE actor_name LIKE %s OR actor_name LIKE %s", ("pytest-%", "%" + MAIL))
    oidc.reset_cache()


def person(env, role="operator", register=True):
    p = users.create_user("pytest-" + uuid.uuid4().hex[:8], role)
    email = "u" + uuid.uuid4().hex[:8] + MAIL
    if register:
        r = env["client"].post("/users/%s/identities" % p["id"], headers=BOOT, json={"email": email})
        assert r.status_code == 200, r.text
    return p, email


def start(env):
    r = env["client"].get("/auth/login", follow_redirects=False)
    assert r.status_code == 302, r.text
    return parse_qs(urlsplit(r.headers["location"]).query)


def finish(env, q, subject, email, code="one-time-code", **claims):
    env["idp"].id_token = env["idp"].make(q["nonce"][0], subject, email, **claims)
    return env["client"].get("/auth/callback", params={"code": code, "state": q["state"][0]}, follow_redirects=False)


def session_of(r):
    loc = r.headers["location"]
    assert loc.startswith("/office#session=rls_"), loc
    return loc.split("#session=", 1)[1]


def sign_in(env, subject="sub-1", **kw):
    p, email = person(env, **kw)
    return p, email, session_of(finish(env, start(env), subject, email))


# ---- off by default ----

def test_without_the_settings_single_sign_on_is_off(env, monkeypatch):
    monkeypatch.delenv("OIDC_ISSUER")
    monkeypatch.delenv("OIDC_CLIENT_ID")
    monkeypatch.delenv("OIDC_CLIENT_SECRET")
    monkeypatch.delenv("OIDC_REDIRECT_URL")
    c = env["client"]
    assert c.get("/auth/config").json() == {"sso": False, "login_url": None}
    assert c.get("/auth/login", follow_redirects=False).status_code == 404
    r = c.get("/auth/callback", params={"code": "x", "state": "y" * 20}, follow_redirects=False)
    assert r.status_code == 302 and "sso_error=" in r.headers["location"]
    p = users.create_user("pytest-" + uuid.uuid4().hex[:8], "operator")
    assert c.post("/users/%s/identities" % p["id"], headers=BOOT, json={"email": "a" + MAIL}).status_code == 409


def test_the_public_routes_need_no_key_and_the_rest_still_do(env):
    c = env["client"]
    assert c.get("/auth/config").json() == {"sso": True, "login_url": "/auth/login"}
    assert c.get("/auth/login", follow_redirects=False).status_code == 302
    assert c.get("/me").status_code == 401 and c.post("/auth/logout").status_code == 401
    assert c.get("/users/%s/identities" % uuid.uuid4()).status_code == 401


# ---- the sign-in ----

def test_a_registered_person_signs_in_and_the_session_works_like_a_key(env):
    p, email, token = sign_in(env)
    me = env["client"].get("/me", headers={"x-api-key": token})
    assert me.status_code == 200 and me.json()["name"] == p["name"] and me.json()["role"] == "operator" and me.json()["kind"] == "user"
    assert env["client"].get("/users", headers={"x-api-key": token}).status_code == 403          # roles apply as for any key
    row = _sql("SELECT token_hash FROM user_sessions WHERE user_id = %s", (p["id"],))[0][0]
    assert row == hashlib.sha256(token.encode()).hexdigest() and token not in row                # only the hash is stored


def test_the_login_address_and_the_redirect_are_safe(env):
    r = env["client"].get("/auth/login", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(ISSUER + "/authorize?") and r.headers["cache-control"] == "no-store"
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == [CLIENT] and len(q["state"][0]) >= 32
    assert "x" * 12 not in r.headers["location"]
    pending = _sql("SELECT verifier FROM sso_logins")
    assert len(pending) == 1 and pending[0][0] not in r.headers["location"]                     # the verifier never leaves the server


def test_the_first_sign_in_binds_the_subject_and_later_email_changes_do_not_matter(env):
    p, email = person(env)
    session_of(finish(env, start(env), "sub-A", email))
    assert _sql("SELECT subject FROM user_identities WHERE user_id = %s", (p["id"],))[0][0] == "sub-A"
    assert session_of(finish(env, start(env), "sub-A", "renamed" + MAIL))                       # same subject, new email: still this person
    r = finish(env, start(env), "sub-B", email)                                                  # same email, different subject: refused
    assert "sso_error=" in r.headers["location"] and "registered" in r.headers["location"]


def test_unknown_people_and_disabled_people_are_refused_with_the_same_message(env):
    r = finish(env, start(env), "sub-X", "nobody" + MAIL)
    unknown = r.headers["location"]
    p, email = person(env)
    users.update_user(p["id"], status="disabled")
    disabled = finish(env, start(env), "sub-Y", email).headers["location"]
    assert unknown == disabled and "sso_error=" in unknown
    assert not _sql("SELECT 1 FROM user_sessions")


def test_a_sign_in_link_works_once_and_expires(env):
    p, email = person(env)
    q = start(env)
    assert session_of(finish(env, q, "sub-1", email))
    again = finish(env, q, "sub-1", email)                                                       # replayed callback
    assert "sso_error=" in again.headers["location"] and "already used" in unquote(again.headers["location"])
    q2 = start(env)
    _sql("UPDATE sso_logins SET expires_at = now() - interval '1 minute'")
    assert "sso_error=" in finish(env, q2, "sub-1", email).headers["location"]
    r = env["client"].get("/auth/callback", params={"code": "c", "state": "no-such-state-at-all"}, follow_redirects=False)
    assert "sso_error=" in r.headers["location"]


def test_the_provider_refusing_or_a_bad_token_gives_an_error_and_no_session(env):
    p, email = person(env)
    q = start(env)
    r = env["client"].get("/auth/callback", params={"error": "access_denied", "state": q["state"][0]}, follow_redirects=False)
    assert "sso_error=" in r.headers["location"] and not _sql("SELECT 1 FROM user_sessions")
    for change in ({"aud": "someone-else"}, {"exp": 1000, "iat": 900}, {"nonce": "another-nonce-value-0123456789"}):
        r = finish(env, start(env), "sub-1", email, **change)
        assert "sso_error=" in r.headers["location"]
    assert not _sql("SELECT 1 FROM user_sessions")


def test_the_second_factor_rule_can_be_required(env, monkeypatch):
    monkeypatch.setenv("OIDC_REQUIRE_MFA", "true")
    p, email = person(env)
    r = finish(env, start(env), "sub-1", email, amr=["pwd"])
    assert "sso_error=" in r.headers["location"] and "second" in r.headers["location"]
    assert session_of(finish(env, start(env), "sub-1", email, amr=["pwd", "otp"]))


def test_too_many_pending_sign_ins_are_refused(env, monkeypatch):
    monkeypatch.setattr(sso, "MAX_PENDING", 3)
    for _ in range(3):
        assert env["client"].get("/auth/login", follow_redirects=False).status_code == 302
    assert env["client"].get("/auth/login", follow_redirects=False).status_code == 502
    _sql("UPDATE sso_logins SET expires_at = now() - interval '1 minute'")
    assert env["client"].get("/auth/login", follow_redirects=False).status_code == 302          # expired ones are cleaned up


# ---- sessions ----

def test_a_session_ends_when_idle_expired_logged_out_revoked_or_the_person_is_disabled(env):
    c = env["client"]
    def ok(tok): return c.get("/me", headers={"x-api-key": tok}).status_code == 200
    p, email, t1 = sign_in(env, "s1")
    assert ok(t1)
    _sql("UPDATE user_sessions SET last_used_at = now() - interval '31 minutes' WHERE user_id = %s", (p["id"],))
    assert not ok(t1)                                                                            # idle for too long
    p2, e2 = person(env)
    t2 = session_of(finish(env, start(env), "s2", e2))
    _sql("UPDATE user_sessions SET expires_at = now() - interval '1 second' WHERE user_id = %s", (p2["id"],))
    assert not ok(t2)                                                                            # absolute limit
    p3, e3 = person(env)
    t3 = session_of(finish(env, start(env), "s3", e3))
    assert c.post("/auth/logout", headers={"x-api-key": t3}).json() == {"signed_out": True} and not ok(t3)
    p4, e4 = person(env)
    t4 = session_of(finish(env, start(env), "s4", e4))
    assert ok(t4)
    users.update_user(p4["id"], status="disabled")
    assert not ok(t4)                                                                            # at once, no cache


def test_the_limits_can_be_set_by_the_owner(env, monkeypatch):
    monkeypatch.setenv("SSO_SESSION_HOURS", "2")
    p, email, token = sign_in(env)
    hours = _sql("SELECT extract(epoch FROM (expires_at - created_at)) / 3600 FROM user_sessions WHERE user_id = %s", (p["id"],))[0][0]
    assert 1.99 < float(hours) < 2.01
    monkeypatch.setenv("SSO_SESSION_HOURS", "9999")
    assert sso.session_hours() == 168 and sso.idle_minutes() == 30
    monkeypatch.setenv("SSO_IDLE_MINUTES", "junk")
    assert sso.idle_minutes() == 30


def test_a_personal_key_cannot_use_the_sign_out_route_and_garbage_tokens_are_refused(env):
    p = users.create_user("pytest-" + uuid.uuid4().hex[:8], "operator")
    key = users.create_key(p["id"], "t")["key"]
    assert env["client"].post("/auth/logout", headers={"x-api-key": key}).status_code == 400
    for bad in ("rls_" + "x" * 200, "rls_doesnotexist", "rls_"):
        assert env["client"].get("/me", headers={"x-api-key": bad}).status_code == 401
    assert sso.resolve_session(None) is None and sso.resolve_session("rlk_abc") is None


def test_a_session_lookup_never_raises(env, monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(sso, "_connect", boom)
    assert sso.resolve_session("rls_" + "a" * 40) is None


# ---- the owner's routes ----

def test_only_the_owner_manages_identities_and_sessions(env):
    p, email = person(env, register=False)
    op = users.create_user("pytest-" + uuid.uuid4().hex[:8], "operator")
    op_key = users.create_key(op["id"], "t")["key"]
    c = env["client"]
    path = "/users/%s/identities" % p["id"]
    assert c.post(path, headers={"x-api-key": op_key}, json={"email": email}).status_code == 403
    assert c.get(path, headers={"x-api-key": op_key}).status_code == 403
    assert c.delete("/users/%s/sessions" % p["id"], headers={"x-api-key": op_key}).status_code == 403
    made = c.post(path, headers=BOOT, json={"email": email.upper()})
    assert made.status_code == 200 and made.json()["email"] == email and made.json()["bound"] is False
    assert c.post(path, headers=BOOT, json={"email": email}).status_code == 409
    listed = c.get(path, headers=BOOT).json()["identities"]
    assert [i["email"] for i in listed] == [email] and "subject" not in json.dumps(listed)
    assert c.delete("%s/%s" % (path, made.json()["id"]), headers=BOOT).status_code == 200
    assert c.delete("%s/%s" % (path, made.json()["id"]), headers=BOOT).status_code == 404


def test_bad_input_to_the_identity_routes(env):
    p = users.create_user("pytest-" + uuid.uuid4().hex[:8], "operator")
    c = env["client"]
    for bad in ("", "no-at-sign", "a b@c.d", "x@" + "y" * 200):
        assert c.post("/users/%s/identities" % p["id"], headers=BOOT, json={"email": bad}).status_code == 400
    assert c.post("/users/%s/identities" % uuid.uuid4(), headers=BOOT, json={"email": "a" + MAIL}).status_code == 404
    users.update_user(p["id"], status="disabled")
    assert c.post("/users/%s/identities" % p["id"], headers=BOOT, json={"email": "b" + MAIL}).status_code == 409


def test_revoking_all_sessions_of_a_person_ends_them(env):
    p, email, token = sign_in(env)
    assert env["client"].delete("/users/%s/sessions" % p["id"], headers=BOOT).json() == {"revoked": 1}
    assert env["client"].get("/me", headers={"x-api-key": token}).status_code == 401


# ---- audit ----

def test_sign_ins_are_recorded_by_name_and_failures_too(env):
    p, email = person(env)
    session_of(finish(env, start(env), "sub-1", email))
    finish(env, start(env), "sub-9", "stranger" + MAIL)
    events = _sql("SELECT actor_name, actor_kind, path, status FROM audit_events WHERE actor_name = %s OR actor_name = %s ORDER BY id",
                  (p["name"], "stranger" + MAIL))
    assert ("%s" % p["name"], "user", "/auth/callback", 200) in events
    assert ("stranger" + MAIL, "sso", "/auth/callback", 403) in events


def test_personal_keys_and_the_shared_key_still_work(env):
    p = users.create_user("pytest-" + uuid.uuid4().hex[:8], "owner")
    key = users.create_key(p["id"], "t")["key"]
    c = env["client"]
    assert c.get("/me", headers={"x-api-key": key}).json()["name"] == p["name"]
    assert c.get("/me", headers=BOOT).json()["kind"] == "bootstrap"
