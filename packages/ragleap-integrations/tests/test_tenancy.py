"""Multi-tenant tests: isolation is asserted through the one network seam, never assumed."""
import json
import threading
import time

import pytest

from ragleap_integrations import McpClient, McpConfig, McpConfigError, McpDiscoveryError, McpPolicy, McpServerConfig
from ragleap_integrations import tenancy
from ragleap_integrations._net import HttpResponse
from ragleap_integrations.tenancy import HAS_SECURITY, McpHub, RateLimit, Scope, ScopeError, _FallbackScope, _FallbackScopeError

URL = "https://mcp.example.com/mcp"
SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}}
ALL = ["get_order", "delete_order", "list_orders"]
ACME = Scope(tenant="acme", subject="u1")
GLOBEX = Scope(tenant="globex", subject="u9")


class Server:
    """The fake MCP server and the recording transport in one."""

    def __init__(self, tools_by_auth=None, delay=0.0):
        self.requests = []
        self.tools_by_auth = tools_by_auth or {}
        self.delay = delay
        self.lock = threading.Lock()
        self.active = {}
        self.max_active = {}

    def __call__(self, url, headers, body, stop=None):
        r = json.loads(body)
        auth = headers.get("Authorization")
        args = (r.get("params") or {}).get("arguments") or {}
        with self.lock:
            self.requests.append({"auth": auth, "method": r["method"], "id": args.get("id")})
            self.active[auth] = self.active.get(auth, 0) + 1
            self.max_active[auth] = max(self.max_active.get(auth, 0), self.active[auth])
        if self.delay:
            time.sleep(self.delay)
        if r["method"] == "tools/list":
            names = self.tools_by_auth.get(auth, ALL)
            result = {"tools": [{"name": n, "description": "d", "inputSchema": SCHEMA} for n in names]}
        else:
            result = {"content": [{"type": "text", "text": "ok"}]}
        with self.lock:
            self.active[auth] -= 1
        return HttpResponse(200, {}, json.dumps({"jsonrpc": "2.0", "id": r["id"], "result": result}).encode(), "application/json")

    def calls(self, auth=None):
        return [x for x in self.requests if x["method"] == "tools/call" and (auth is None or x["auth"] == auth)]

    def lists(self):
        return [x for x in self.requests if x["method"] == "tools/list"]


def cfg(**kw):
    return McpConfig(servers=[McpServerConfig("g", URL)], allowed_tools=["g." + t for t in ALL], **kw)


def by_tenant(scope):
    return "example-" + scope.tenant


def hub(server, **kw):
    kw.setdefault("token_provider", lambda scope, srv: by_tenant(scope))
    return McpHub(kw.pop("config", None) or cfg(), transport=server, **kw)


def call(conn, tool="get_order", **args):
    return conn.toolkit.call("g__" + tool, json.dumps(args or {"id": "1"}))


# ---------------------------------------------------------------- scopes

@pytest.mark.parametrize("bad", [None, "acme", {"tenant": "acme"}, object(), 7])
def test_a_scope_is_required(bad):
    with pytest.raises(ScopeError):
        hub(Server()).connect(bad)


def test_a_subclass_of_scope_is_refused():
    class Sub(Scope):
        pass
    with pytest.raises(ScopeError):
        hub(Server()).connect(Sub(tenant="acme"))


def test_require_scope_returns_the_scope():
    assert tenancy.require_scope(ACME) is ACME


@pytest.mark.skipif(not HAS_SECURITY, reason="ragleap-security not installed")
def test_the_security_scope_is_the_one_in_use_and_the_fallback_is_refused():
    import ragleap_security
    assert Scope is ragleap_security.Scope
    with pytest.raises(ScopeError):
        hub(Server()).connect(_FallbackScope(tenant="acme"))


def test_the_fallback_scope_has_the_same_rules_and_key_format():
    s = _FallbackScope(tenant="acme", subject="u1", session="s.1")
    assert s.key == "tenant=acme/subject=u1/session=s.1"
    assert _FallbackScope(tenant="acme").key == "tenant=acme"
    for bad in ({"tenant": ""}, {"tenant": "a/b"}, {"tenant": "a=b"}, {"tenant": "-x"}, {"tenant": "x" * 129},
                {"tenant": "acme", "subject": ""}, {"tenant": "acme", "agent": 5}):
        with pytest.raises(_FallbackScopeError):
            _FallbackScope(**bad)
    with pytest.raises(Exception):
        s.tenant = "other"  # frozen


@pytest.mark.skipif(not HAS_SECURITY, reason="ragleap-security not installed")
def test_the_fallback_key_equals_the_security_key():
    import ragleap_security
    kw = {"tenant": "acme", "subject": "u1", "agent": "a", "session": "s"}
    assert _FallbackScope(**kw).key == ragleap_security.Scope(**kw).key


def test_the_hub_works_with_the_fallback_scope_when_it_is_the_scope(monkeypatch):
    monkeypatch.setattr(tenancy, "Scope", _FallbackScope)
    monkeypatch.setattr(tenancy, "ScopeError", _FallbackScopeError)
    srv = Server()
    conn = hub(srv).connect(_FallbackScope(tenant="acme"))
    assert call(conn).success is True
    with pytest.raises(_FallbackScopeError):
        hub(srv).connect("acme")


# ---------------------------------------------------------------- construction

def test_hub_construction_is_validated():
    c = cfg()
    for kw in ({"token_provider": "x"}, {"policy_for": 3}, {"allow_for": []}, {"limit": (1, 2)}, {"audit": object()},
               {"max_scopes": 0}, {"max_scopes": True}, {"max_scopes": "5"}):
        with pytest.raises(McpConfigError):
            McpHub(c, **kw)
    with pytest.raises(McpConfigError):
        McpHub("not a config")


def test_ratelimit_is_validated():
    for a, b in ((0, 1), (-1, 1), (True, 1), (1.5, 1), (1, 0), (1, -2), (1, "x"), (1, True)):
        with pytest.raises(McpConfigError):
            RateLimit(a, b)
    assert RateLimit(2, 0.5).calls == 2


def test_approve_scoped_must_be_callable():
    with pytest.raises(ValueError):
        McpPolicy(approve_scoped="yes")


# ---------------------------------------------------------------- connections and cache

def test_connect_builds_tools_for_the_scope_and_caches_it():
    srv = Server()
    h = hub(srv)
    conn = h.connect(ACME)
    assert [t.name for t in conn.tools] == ["g__get_order", "g__delete_order", "g__list_orders"]
    assert h.connect(ACME) is conn
    assert len(srv.lists()) == 1
    assert h.connect(Scope(tenant="acme", subject="u2")) is not conn
    assert h.connect(GLOBEX) is not conn


def test_each_scope_has_its_own_client_and_snapshot():
    srv = Server()
    h = hub(srv)
    a, b = h.connect(ACME), h.connect(GLOBEX)
    assert a.client is not b.client and a.toolkit is not b.toolkit and a.tools[0] is not b.tools[0]
    assert a.client._eras is not b.client._eras


def test_the_owner_config_is_never_changed():
    srv = Server()
    c = cfg(policy=McpPolicy(pre_approved=["g.get_order"]))
    before = (list(c.allowed_tools), list(c.policy.pre_approved), c.on_event)
    h = McpHub(c, transport=srv, allow_for=lambda s: ["g.delete_order"], policy_for=lambda s: McpPolicy(pre_approved=["g.delete_order"]))
    h.connect(ACME)
    assert (list(c.allowed_tools), list(c.policy.pre_approved), c.on_event) == before


def test_concurrent_first_connects_for_one_scope_build_once():
    srv = Server(delay=0.02)
    h = hub(srv)
    out = []
    ts = [threading.Thread(target=lambda: out.append(h.connect(ACME))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len({id(c) for c in out}) == 1 and len(srv.lists()) == 1


def test_least_recently_used_scope_is_dropped():
    srv = Server()
    h = hub(srv, max_scopes=2)
    a = h.connect(Scope(tenant="a1"))
    h.connect(Scope(tenant="b1"))
    assert h.connect(Scope(tenant="a1")) is a  # touching a1 makes b1 the oldest
    h.connect(Scope(tenant="c1"))
    assert h.scopes == ["tenant=a1", "tenant=c1"]
    assert h.connect(Scope(tenant="b1")) is not None and len(srv.lists()) == 4


def test_forget_one_scope_or_a_whole_tenant():
    srv = Server()
    h = hub(srv)
    s1, s2 = Scope(tenant="acme", subject="u1"), Scope(tenant="acme", subject="u2")
    c1 = h.connect(s1)
    h.connect(s2)
    h.connect(GLOBEX)
    assert h.forget(s1) == 1 and h.forget(s1) == 0
    assert h.connect(s1) is not c1
    assert h.forget(tenant="acme") == 2 and h.scopes == ["tenant=globex/subject=u9"]
    for bad in ((), (ACME, "acme")):
        with pytest.raises(ValueError):
            h.forget(*bad[:1], **({"tenant": bad[1]} if len(bad) > 1 else {}))
    with pytest.raises(ScopeError):
        h.forget("acme")


# ---------------------------------------------------------------- tokens

def test_a_tenants_token_is_never_sent_for_another_tenant():
    srv = Server()
    h = hub(srv)
    a, b = h.connect(ACME), h.connect(GLOBEX)
    for _ in range(3):
        call(a, id="A")
        call(b, id="B")
    for r in srv.requests:
        if r["id"] == "A":
            assert r["auth"] == "Bearer example-acme"
        if r["id"] == "B":
            assert r["auth"] == "Bearer example-globex"
    assert {r["auth"] for r in srv.lists()} == {"Bearer example-acme", "Bearer example-globex"}
    assert len(srv.calls("Bearer example-acme")) == 3 and len(srv.calls("Bearer example-globex")) == 3


def test_the_provider_is_called_for_every_request_and_nothing_is_cached():
    srv = Server()
    seen = []
    state = {"tok": "example-one"}

    def provider(scope, server):
        seen.append((scope, server))
        return state["tok"]

    conn = hub(srv, token_provider=provider).connect(ACME)
    n = len(seen)
    assert n >= 1 and all(s == (ACME, "g") for s in seen)
    call(conn)
    assert len(seen) > n
    state["tok"] = "example-two"
    call(conn)
    assert srv.calls()[-1]["auth"] == "Bearer example-two" and srv.calls()[0]["auth"] == "Bearer example-one"


def test_no_provider_keeps_the_static_token():
    srv = Server()
    c = McpConfig(servers=[McpServerConfig("g", URL, token="example-static-1")], allowed_tools=["g.get_order"])
    conn = McpHub(c, transport=srv).connect(ACME)
    call(conn)
    assert {r["auth"] for r in srv.requests} == {"Bearer example-static-1"}


def test_a_provider_returning_none_sends_no_token_and_ignores_the_static_one():
    srv = Server()
    c = McpConfig(servers=[McpServerConfig("g", URL, token="example-static-1")], allowed_tools=["g.get_order"])
    conn = McpHub(c, transport=srv, token_provider=lambda s, n: None).connect(ACME)
    call(conn)
    assert {r["auth"] for r in srv.requests} == {None}


def test_a_wrapped_token_is_revealed_only_for_the_header():
    class Wrapped:
        def __init__(self, v):
            self._v = v

        def reveal(self):
            return self._v

        def __repr__(self):
            return "Wrapped(***)"

    srv = Server()
    conn = hub(srv, token_provider=lambda s, n: Wrapped("example-hidden-5")).connect(ACME)
    call(conn)
    assert {r["auth"] for r in srv.requests} == {"Bearer example-hidden-5"}


@pytest.mark.skipif(not HAS_SECURITY, reason="ragleap-security not installed")
def test_the_real_wrapper_from_ragleap_security_works():
    from ragleap_security import SecretStr
    srv = Server()
    conn = hub(srv, token_provider=lambda s, n: SecretStr("example-real-9")).connect(ACME)
    call(conn)
    assert {r["auth"] for r in srv.requests} == {"Bearer example-real-9"}


@pytest.mark.parametrize("value", ["", "has\nnewline", "tab\there", 5, ["x"], b"bytes"])
def test_unusable_tokens_give_the_constant_message_and_nothing_is_sent(value):
    srv = Server()
    state = {"v": "example-ok"}
    conn = hub(srv, token_provider=lambda s, n: state["v"]).connect(ACME)
    n = len(srv.requests)
    state["v"] = value
    r = call(conn)
    assert r.success is False and r.error == "MCP token is unavailable"
    assert len(srv.requests) == n


def test_a_failing_provider_gives_the_constant_message_without_its_text():
    srv = Server()
    state = {"fail": False}

    def provider(scope, server):
        if state["fail"]:
            raise RuntimeError("example-private-detail-1")
        return "example-ok"

    conn = hub(srv, token_provider=provider).connect(ACME)
    state["fail"] = True
    n = len(srv.requests)
    r = call(conn)
    assert r.success is False and r.error == "MCP token is unavailable" and "example-private-detail" not in repr(r)
    assert len(srv.requests) == n


def test_a_failing_provider_during_discovery_is_a_constant_discovery_error():
    def provider(scope, server):
        raise RuntimeError("example-private-detail-1")

    with pytest.raises(McpDiscoveryError) as e:
        hub(Server(), token_provider=provider).connect(ACME)
    assert "example-private-detail" not in str(e.value) and "MCP token is unavailable" in str(e.value)


def test_the_token_never_appears_in_events_audit_results_errors_or_the_report():
    srv = Server()
    events, audit = [], []

    class Audit:
        def append(self, event, data=None, scope=None):
            audit.append((event, data, scope))

    h = hub(srv, token_provider=lambda s, n: "example-leakcheck-31", audit=Audit(),
            config=cfg(on_event=events.append))
    conn = h.connect(ACME)
    r = call(conn)
    r2 = call(conn, "nope")
    blob = json.dumps([events, audit], default=str) + repr(r) + repr(r2) + conn.report()
    assert "example-leakcheck" not in blob


# ---------------------------------------------------------------- discovery per tenant

def test_each_tenant_discovers_with_its_own_token_and_sees_its_own_tools():
    srv = Server(tools_by_auth={"Bearer example-acme": ["get_order"], "Bearer example-globex": ["get_order", "list_orders"]})
    h = hub(srv)
    a, b = h.connect(ACME), h.connect(GLOBEX)
    assert [t.name for t in a.tools] == ["g__get_order"]
    assert [t.name for t in b.tools] == ["g__get_order", "g__list_orders"]
    assert ("g.delete_order", "not offered by the server") in a.skipped


# ---------------------------------------------------------------- allowlists

def test_allow_for_narrows_and_never_widens():
    srv = Server()
    h = hub(srv, allow_for=lambda s: ["g.get_order", "g.evil"] if s.tenant == "acme" else None)
    a, b = h.connect(ACME), h.connect(GLOBEX)
    assert [t.name for t in a.tools] == ["g__get_order"] and len(b.tools) == 3
    assert a.client.call_tool("g", "delete_order", {"id": "1"}).error == "MCP tool is not allowlisted"
    assert a.client.call_tool("g", "evil", {"id": "1"}).error == "MCP tool is not allowlisted"
    assert not srv.calls(auth="Bearer example-acme")


def test_allow_for_with_nothing_left_or_a_bad_value_is_a_config_error():
    for value in ([], ["g.unknown"], "g.get_order", 5):
        with pytest.raises(McpConfigError):
            hub(Server(), allow_for=lambda s, v=value: v).connect(ACME)


def test_a_failing_allow_for_or_policy_for_is_a_constant_config_error():
    def boom(scope):
        raise RuntimeError("example-private-detail-2")

    for kw in ({"allow_for": boom}, {"policy_for": boom}):
        with pytest.raises(McpConfigError) as e:
            hub(Server(), **kw).connect(ACME)
        assert "example-private-detail" not in str(e.value)


# ---------------------------------------------------------------- per-tenant policy

def test_policy_for_gives_each_tenant_its_own_approval_rule():
    srv = Server()
    deny = McpPolicy(approve=lambda s, t, a: False)
    allow = McpPolicy(approve=lambda s, t, a: True)
    h = hub(srv, policy_for=lambda sc: deny if sc.tenant == "acme" else allow)
    a, b = h.connect(ACME), h.connect(GLOBEX)
    assert call(a, "delete_order").error == "MCP call was not approved"
    assert call(b, "delete_order").success is True
    assert not srv.calls("Bearer example-acme") and len(srv.calls("Bearer example-globex")) == 1


def test_policy_for_none_uses_the_owner_policy_and_pre_approved_is_filtered_to_the_tenant():
    srv = Server()
    owner = McpPolicy(approve=lambda s, t, a: False, pre_approved=["g.get_order", "g.delete_order"])
    h = hub(srv, config=cfg(policy=owner), allow_for=lambda s: ["g.get_order"], policy_for=lambda s: None)
    conn = h.connect(ACME)
    assert call(conn, "get_order").success is True
    assert conn.config.policy.pre_approved == ["g.get_order"]


def test_policy_for_must_return_a_policy_or_none():
    with pytest.raises(McpConfigError):
        hub(Server(), policy_for=lambda s: "yes").connect(ACME)


def test_approve_scoped_receives_the_scope_and_wins_over_approve():
    srv = Server()
    got = []

    def scoped(scope, server, tool, arguments):
        got.append((scope, server, tool, arguments))
        return True

    pol = McpPolicy(approve=lambda s, t, a: False, approve_scoped=scoped)
    conn = hub(srv, policy_for=lambda s: pol).connect(ACME)
    assert call(conn, "delete_order", id="7").success is True
    assert got == [(ACME, "g", "delete_order", {"id": "7"})] and got[0][0] is ACME


def test_approve_scoped_denies_on_exception_on_non_true_and_without_a_scope():
    srv = Server()
    for answer in (RuntimeError("x"), "yes", 1, None, False):
        def scoped(scope, server, tool, arguments, a=answer):
            if isinstance(a, Exception):
                raise a
            return a
        conn = hub(srv, policy_for=lambda s, f=scoped: McpPolicy(approve_scoped=f)).connect(Scope(tenant="t%d" % id(answer)))
        assert call(conn, "delete_order").error == "MCP call was not approved"
    plain = McpClient(cfg(policy=McpPolicy(approve_scoped=lambda *a: True)), transport=srv)  # no scope given
    assert plain.call_tool("g", "delete_order", {"id": "1"}, requires_approval=True).error == "MCP call was not approved"
    assert not srv.calls()


# ---------------------------------------------------------------- rate limits

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_the_limit_is_per_tenant_counted_before_the_network_and_slides():
    srv, clk = Server(), Clock()
    h = hub(srv, limit=RateLimit(2, 10), clock=clk)
    a1, a2 = h.connect(ACME), h.connect(Scope(tenant="acme", subject="u2"))
    b = h.connect(GLOBEX)
    assert call(a1).success and call(a2).success          # two subjects, one tenant budget
    r = call(a1)
    assert r.success is False and r.error == "MCP call limit reached"
    assert len(srv.calls("Bearer example-acme")) == 2         # the denied call never reached the server
    assert call(b).success and call(b).success and call(b).error == "MCP call limit reached"
    clk.t += 10.0                                          # exactly one window later
    assert call(a1).success


def test_invalid_calls_do_not_use_the_budget():
    srv = Server()
    h = hub(srv, limit=RateLimit(1, 60), clock=Clock())
    conn = h.connect(ACME)
    assert conn.client.call_tool("g", "evil", {"id": "1"}).error == "MCP tool is not allowlisted"
    assert conn.client.call_tool("g", "get_order", "not a dict").error == "MCP tool arguments are not valid"
    assert call(conn).success is True


def test_forgetting_a_tenant_resets_its_window():
    srv, clk = Server(), Clock()
    h = hub(srv, limit=RateLimit(1, 60), clock=clk)
    assert call(h.connect(ACME)).success and call(h.connect(ACME)).success is False
    h.forget(tenant="acme")
    assert call(h.connect(ACME)).success is True


def test_a_failing_guard_denies_the_call():
    srv = Server()
    c = McpClient(cfg(), transport=srv, guard=lambda: 1 / 0)
    assert c.call_tool("g", "get_order", {"id": "1"}).error == "MCP call limit reached"
    assert not srv.calls()


# ---------------------------------------------------------------- events and audit

def test_events_carry_the_scope_and_never_arguments_or_results():
    srv, events = Server(), []
    h = hub(srv, config=cfg(on_event=events.append))
    conn = h.connect(ACME)
    call(conn, id="needle-123")
    assert [e["event"] for e in events] == ["discover", "call"]
    for e in events:
        assert e["tenant"] == "acme" and e["scope"] == ACME.key
        assert "needle-123" not in json.dumps(e) and "arguments" not in e and "result" not in e


def test_a_client_without_a_scope_has_no_tenant_fields():
    srv, events = Server(), []
    McpClient(cfg(on_event=events.append), transport=srv).discover()
    assert events and "tenant" not in events[0] and "scope" not in events[0]


def test_audit_receives_every_event_with_the_exact_scope():
    srv, rec = Server(), []

    class Audit:
        def append(self, event, data=None, scope=None):
            rec.append((event, data, scope))

    conn = hub(srv, audit=Audit()).connect(ACME)
    call(conn, id="needle-123")
    assert [r[0] for r in rec] == ["discover", "call"]
    assert all(r[2] is ACME for r in rec) and "event" not in rec[1][1] and rec[1][1]["tool"] == "get_order"
    assert "needle-123" not in json.dumps(rec, default=str)


def test_a_failing_audit_or_owner_hook_never_breaks_a_call_and_the_other_still_runs():
    srv, seen = Server(), []

    class Audit:
        def append(self, event, data=None, scope=None):
            seen.append(event)
            raise RuntimeError("disk full")

    def owner(event):
        seen.append("owner:" + event["event"])
        raise RuntimeError("hook broke")

    conn = hub(srv, audit=Audit(), config=cfg(on_event=owner)).connect(ACME)
    assert call(conn).success is True
    assert seen == ["owner:discover", "discover", "owner:call", "call"]


@pytest.mark.skipif(not HAS_SECURITY, reason="ragleap-security not installed")
def test_events_land_in_a_real_hash_chained_audit_log(tmp_path):
    from ragleap_security import AuditLog
    path = str(tmp_path / "audit.jsonl")
    log = AuditLog(path)
    srv = Server()
    h = hub(srv, audit=log)
    call(h.connect(ACME))
    call(h.connect(GLOBEX))
    assert log.verify().ok
    text = open(path, encoding="utf-8").read()
    assert "tenant=acme" in text and "tenant=globex" in text and "example-" not in text


# ---------------------------------------------------------------- threads

def test_concurrent_tenants_never_cross_tokens():
    srv = Server(delay=0.002)
    h = hub(srv)
    scopes = [ACME, GLOBEX]
    conns = {s.tenant: h.connect(s) for s in scopes}

    def work(s):
        for _ in range(15):
            call(conns[s.tenant], id=s.tenant)

    ts = [threading.Thread(target=work, args=(s,)) for s in scopes for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    calls = srv.calls()
    assert len(calls) == 120
    assert all(c["auth"] == "Bearer example-" + c["id"] for c in calls)


def test_requests_of_one_scope_are_serialized_and_scopes_do_not_share_a_lock():
    srv = Server(delay=0.01)
    h = hub(srv)
    conn = h.connect(ACME)
    ts = [threading.Thread(target=call, args=(conn,)) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert srv.max_active["Bearer example-acme"] == 1
    assert h.connect(ACME).client._lock is not h.connect(GLOBEX).client._lock


# ---------------------------------------------------------------- nothing changes without a hub

def test_a_plain_client_is_unchanged():
    srv = Server()
    c = McpConfig(servers=[McpServerConfig("g", URL, token="example-static-1")], allowed_tools=["g.get_order"])
    client = McpClient(c, transport=srv)
    assert client._token_for is None and client._scope is None and client._guard is None and client._lock is None
    assert client.call_tool("g", "get_order", {"id": "1"}).success is True
    assert srv.requests[-1]["auth"] == "Bearer example-static-1"
