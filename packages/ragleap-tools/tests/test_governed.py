"""GovernedToolkit: scope, allowlist, rate limit, approval and audit events."""
import logging
import threading

import pytest

from ragleap_tools import Tool, ToolError, ToolResult, Toolkit
from ragleap_tools.governed import (
    GovernanceConfigError,
    GovernedToolkit,
    RateLimit,
    Scope,
    ScopeError,
    _FallbackScope,
    _FallbackScopeError,
)

ACME = Scope(tenant="acme", subject="alice")
OTHER = Scope(tenant="other")


def tool(name="echo", handler=None, props=None, required=None):
    return Tool(
        name=name,
        description="d",
        parameters={"type": "object", "properties": props if props is not None else {"x": {"type": "integer"}},
                    "required": required if required is not None else []},
        handler=handler or (lambda **kw: ToolResult(success=True, result=kw)),
    )


def kit(*tools):
    return Toolkit(list(tools) or [tool("echo"), tool("wipe")])


def counting_wipe(ran, result=1):
    return tool("wipe", handler=lambda **kw: ran.append(1) or ToolResult(success=True, result=result))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def governed(toolkit=None, allow=("echo", "wipe"), **kw):
    toolkit = toolkit or kit()
    return GovernedToolkit(toolkit, allow_for=lambda scope: list(allow), **kw)


# -- configuration ----------------------------------------------------------------------------------------------

def test_a_toolkit_is_required():
    for bad in (None, [], "kit", object()):
        with pytest.raises(GovernanceConfigError, match="toolkit must be a Toolkit"):
            GovernedToolkit(bad, allow_for=lambda s: [])


@pytest.mark.parametrize("bad", [None, "echo", ["echo"], 5])
def test_allow_for_must_be_callable(bad):
    with pytest.raises(GovernanceConfigError, match="allow_for must be callable"):
        GovernedToolkit(kit(), allow_for=bad)


@pytest.mark.parametrize("bad", ["wipe", b"wipe", {"wipe": 1}, 5, None, (n for n in ["wipe"])])
def test_require_approval_must_be_a_list_of_names(bad):
    with pytest.raises(GovernanceConfigError, match="require_approval must be a list of tool names"):
        governed(require_approval=bad)


def test_require_approval_entries_must_be_strings_and_known_tools():
    with pytest.raises(GovernanceConfigError, match="must hold tool names"):
        governed(require_approval=[5])
    with pytest.raises(GovernanceConfigError, match="not in the toolkit: 'wiped'"):
        governed(require_approval=["wiped"])


def test_require_approval_accepts_list_tuple_set_and_frozenset():
    for ok in (["wipe"], ("wipe",), {"wipe"}, frozenset({"wipe"}), []):
        governed(require_approval=ok)


@pytest.mark.parametrize("name, bad, message", [
    ("approve", "yes", "approve must be callable"),
    ("on_event", 5, "on_event must be callable"),
    ("clock", "now", "clock must be callable"),
    ("rate_limit", 5, "rate_limit must be a RateLimit"),
])
def test_other_options_are_checked(name, bad, message):
    with pytest.raises(GovernanceConfigError, match=message):
        governed(**{name: bad})


def test_a_look_alike_or_subclass_of_ratelimit_is_refused():
    class Sub(RateLimit):
        pass

    class Alike:
        calls = 5
        per_seconds = 1

    for bad in (Sub(calls=1, per_seconds=1), Alike()):
        with pytest.raises(GovernanceConfigError, match="rate_limit must be a RateLimit"):
            governed(rate_limit=bad)


@pytest.mark.parametrize("calls", [0, -1, True, 1.5, "3", None])
def test_ratelimit_calls_must_be_a_positive_integer(calls):
    with pytest.raises(GovernanceConfigError, match="calls must be a positive integer"):
        RateLimit(calls=calls, per_seconds=1)


@pytest.mark.parametrize("seconds", [0, -1, -0.5, True, "1", None])
def test_ratelimit_per_seconds_must_be_a_positive_number(seconds):
    with pytest.raises(GovernanceConfigError, match="per_seconds must be a positive number"):
        RateLimit(calls=1, per_seconds=seconds)


def test_ratelimit_accepts_ints_and_floats_and_is_frozen():
    assert RateLimit(calls=1, per_seconds=1).per_seconds == 1
    assert RateLimit(calls=5, per_seconds=0.5).per_seconds == 0.5
    with pytest.raises(Exception):
        RateLimit(calls=1, per_seconds=1).calls = 2


def test_governance_config_error_is_a_value_error():
    assert issubclass(GovernanceConfigError, ValueError)


# -- the built-in scope (same rules as ragleap_security.Scope) ------------------------------------------------------

def test_fallback_scope_key_and_fields():
    s = _FallbackScope(tenant="acme", subject="u1", agent="bot", session="s9")
    assert s.key == "tenant=acme/subject=u1/agent=bot/session=s9"
    assert _FallbackScope(tenant="acme").key == "tenant=acme"
    assert _FallbackScope(tenant="acme", agent="bot").key == "tenant=acme/agent=bot"
    with pytest.raises(Exception):
        s.tenant = "x"


@pytest.mark.parametrize("kwargs, message", [
    ({"tenant": None}, "tenant is required"),
    ({"tenant": ""}, "invalid tenant"),
    ({"tenant": "a/b"}, "invalid tenant"),
    ({"tenant": "a=b"}, "invalid tenant"),
    ({"tenant": "-x"}, "invalid tenant"),
    ({"tenant": "x" * 129}, "invalid tenant"),
    ({"tenant": "caf\u00e9"}, "invalid tenant"),
    ({"tenant": 5}, "invalid tenant"),
    ({"tenant": "ok\n"}, "invalid tenant"),
    ({"tenant": "ok", "subject": "-x"}, "invalid subject"),
    ({"tenant": "ok", "agent": ""}, "invalid agent"),
    ({"tenant": "ok", "session": "a b"}, "invalid session"),
])
def test_fallback_scope_refuses_bad_parts(kwargs, message):
    with pytest.raises(_FallbackScopeError, match=message):
        _FallbackScope(**kwargs)


def test_fallback_scope_accepts_the_edge_cases():
    assert _FallbackScope(tenant="x" * 128).tenant == "x" * 128
    assert _FallbackScope(tenant="a.b_c:d@e-f").key == "tenant=a.b_c:d@e-f"
    assert _FallbackScope(tenant="7up").tenant == "7up"


def test_scope_errors_are_value_errors():
    assert issubclass(_FallbackScopeError, ValueError)
    assert issubclass(ScopeError, ValueError)


def test_the_real_scope_agrees_with_the_fallback():
    security = pytest.importorskip("ragleap_security")
    cases = [
        {"tenant": "acme"},
        {"tenant": "acme", "subject": "u1", "agent": "bot", "session": "s9"},
        {"tenant": None},
        {"tenant": ""},
        {"tenant": "a/b"},
        {"tenant": "a=b"},
        {"tenant": "-x"},
        {"tenant": "x" * 128},
        {"tenant": "x" * 129},
        {"tenant": "caf\u00e9"},
        {"tenant": 5},
        {"tenant": "ok\n"},
        {"tenant": "ok", "subject": "-x"},
        {"tenant": "ok", "agent": ""},
        {"tenant": "ok", "session": "a b"},
        {"tenant": "a.b_c:d@e-f"},
        {"tenant": "7up"},
    ]
    for kw in cases:
        try:
            real = security.Scope(**kw)
            real_out = ("ok", real.key)
        except security.ScopeError as exc:
            real_out = ("error", str(exc))
        try:
            fb = _FallbackScope(**kw)
            fb_out = ("ok", fb.key)
        except _FallbackScopeError as exc:
            fb_out = ("error", str(exc))
        assert real_out == fb_out, kw


# -- scope handling in call() ---------------------------------------------------------------------------------------

def test_call_refuses_anything_that_is_not_exactly_a_scope_and_runs_nothing():
    ran = []
    g = governed(kit(tool("echo", handler=lambda **kw: ran.append(kw) or ToolResult(success=True, result="r"))))

    class Forged(Scope):
        pass

    fake = type("Fake", (), {"tenant": "acme", "key": "k"})()
    for bad in (None, "acme", {"tenant": "acme"}, 5, object(), Forged(tenant="acme"), fake):
        r = g.call(bad, "echo", {})
        assert r == ToolResult(success=False, error="a scope is required")
    assert ran == []


def test_an_invalid_scope_is_reported_without_tenant_fields():
    events = []
    g = governed(on_event=events.append)
    g.call("acme", "echo", {})
    assert len(events) == 1
    e = events[0]
    assert e["error"] == "invalid_scope" and e["ok"] is False and e["tool"] is None and e["approval"] == "unchecked"
    assert "tenant" not in e and "scope" not in e


# -- the allowlist ---------------------------------------------------------------------------------------------------

def test_an_allowed_tool_runs_like_toolkit_call():
    g = governed()
    assert g.call(ACME, "echo", {"x": 1}) == ToolResult(success=True, result={"x": 1})


def test_allow_for_gets_the_scope_and_decides_per_scope():
    seen = []

    def allow(scope):
        seen.append(scope)
        return ["echo", "wipe"] if scope.tenant == "acme" else ["echo"]

    g = GovernedToolkit(kit(), allow_for=allow)
    assert g.call(ACME, "wipe", {}).success is True
    assert g.call(OTHER, "wipe", {}) == ToolResult(success=False, error="tool not available")
    assert g.call(OTHER, "echo", {}).success is True
    assert seen == [ACME, OTHER, OTHER]


@pytest.mark.parametrize("allowed", [["echo"], ("echo",), {"echo"}, frozenset({"echo"})])
def test_allow_for_may_return_a_list_tuple_set_or_frozenset(allowed):
    g = GovernedToolkit(kit(), allow_for=lambda s: allowed)
    assert g.call(ACME, "echo", {}).success is True
    assert g.call(ACME, "wipe", {}).error == "tool not available"


def test_a_tool_that_is_not_named_is_not_available_and_not_run():
    ran = []
    wipe = tool("wipe", handler=lambda **kw: ran.append(1))
    g = GovernedToolkit(kit(tool("echo"), wipe), allow_for=lambda s: ["echo"])
    assert g.call(ACME, "wipe", {}) == ToolResult(success=False, error="tool not available")
    assert ran == []


def test_unknown_and_not_allowed_tools_get_the_same_answer_and_event():
    events = []
    g = GovernedToolkit(kit(), allow_for=lambda s: ["echo"], on_event=events.append)
    a = g.call(ACME, "wipe", {})
    b = g.call(ACME, "does-not-exist", {})
    assert a == b == ToolResult(success=False, error="tool not available")
    strip = lambda e: {k: v for k, v in e.items() if k != "duration_ms"}
    assert strip(events[0]) == strip(events[1])
    assert events[0]["tool"] is None and events[0]["error"] == "not_available"


def test_a_name_that_allow_for_lists_but_the_toolkit_lacks_is_not_available():
    g = GovernedToolkit(kit(), allow_for=lambda s: ["echo", "ghost"])
    assert g.call(ACME, "ghost", {}).error == "tool not available"


@pytest.mark.parametrize("name", [None, 5, ["echo"], b"echo", ("echo",), {"echo"}, 1.5, object()])
def test_a_name_that_is_not_a_string_is_not_available(name):
    assert governed().call(ACME, name, {}) == ToolResult(success=False, error="tool not available")


@pytest.mark.parametrize(
    "returned",
    [None, "echo", b"echo", {"echo": 1}, 5, ["echo", 5], [None], iter(["echo"]), [["echo"]]],
)
def test_allow_for_returning_anything_else_fails_closed(returned):
    events = []
    g = GovernedToolkit(kit(), allow_for=lambda s: returned, on_event=events.append)
    assert g.call(ACME, "echo", {}) == ToolResult(success=False, error="tool not available")
    assert events[0]["error"] == "not_available"


def test_allow_for_raising_fails_closed_and_logs_only_the_exception_type(caplog):
    def allow(scope):
        raise RuntimeError("CANARY-ALLOW internal detail")

    g = GovernedToolkit(kit(), allow_for=allow)
    with caplog.at_level(logging.WARNING, logger="ragleap_tools"):
        r = g.call(ACME, "echo", {})
    assert r == ToolResult(success=False, error="tool not available")
    assert "RuntimeError" in caplog.text
    assert "CANARY-ALLOW" not in caplog.text


def test_the_empty_allowlist_allows_nothing():
    g = GovernedToolkit(kit(), allow_for=lambda s: [])
    assert g.call(ACME, "echo", {}).error == "tool not available"


# -- the rate limit ---------------------------------------------------------------------------------------------------

def test_calls_over_the_limit_are_refused_and_the_handler_does_not_run():
    ran = []
    g = governed(kit(tool("echo", handler=lambda **kw: ran.append(1) or ToolResult(success=True, result=1))),
                 rate_limit=RateLimit(calls=2, per_seconds=60), clock=Clock())
    assert g.call(ACME, "echo", {}).success and g.call(ACME, "echo", {}).success
    assert g.call(ACME, "echo", {}) == ToolResult(success=False, error="rate limit exceeded")
    assert len(ran) == 2


def test_the_window_slides_and_a_hit_exactly_one_window_old_is_dropped():
    clock = Clock()
    g = governed(rate_limit=RateLimit(calls=1, per_seconds=10), clock=clock)
    assert g.call(ACME, "echo", {}).success
    clock.now += 9.999
    assert g.call(ACME, "echo", {}).error == "rate limit exceeded"
    clock.now += 0.001
    assert g.call(ACME, "echo", {}).success


def test_refused_calls_do_not_extend_the_window():
    clock = Clock()
    g = governed(rate_limit=RateLimit(calls=1, per_seconds=10), clock=clock)
    assert g.call(ACME, "echo", {}).success
    for _ in range(5):
        clock.now += 1
        assert g.call(ACME, "echo", {}).error == "rate limit exceeded"
    clock.now += 5
    assert g.call(ACME, "echo", {}).success


def test_tenants_have_separate_windows_and_subjects_of_one_tenant_share_one():
    g = governed(rate_limit=RateLimit(calls=1, per_seconds=60), clock=Clock())
    assert g.call(Scope(tenant="a", subject="u1"), "echo", {}).success
    assert g.call(Scope(tenant="a", subject="u2"), "echo", {}).error == "rate limit exceeded"
    assert g.call(Scope(tenant="b", subject="u1"), "echo", {}).success


def test_forget_resets_one_tenant_or_all_and_counts_the_windows():
    g = governed(rate_limit=RateLimit(calls=1, per_seconds=60), clock=Clock())
    g.call(Scope(tenant="a"), "echo", {})
    g.call(Scope(tenant="b"), "echo", {})
    assert g.forget("a") == 1 and g.forget("a") == 0 and g.forget("never-seen") == 0
    assert g.call(Scope(tenant="a"), "echo", {}).success
    assert g.call(Scope(tenant="b"), "echo", {}).error == "rate limit exceeded"
    assert g.forget() == 2 and g.forget() == 0
    assert g.call(Scope(tenant="b"), "echo", {}).success


def test_calls_that_are_refused_before_the_limit_do_not_use_up_quota():
    g = GovernedToolkit(
        kit(), allow_for=lambda s: ["echo"], rate_limit=RateLimit(calls=1, per_seconds=60), clock=Clock()
    )
    for _ in range(5):
        assert g.call(ACME, "wipe", {}).error == "tool not available"
        assert g.call("nobody", "echo", {}).error == "a scope is required"
    assert g.call(ACME, "echo", {}).success


def test_calls_with_invalid_arguments_do_count_against_the_limit():
    g = governed(rate_limit=RateLimit(calls=1, per_seconds=60), clock=Clock())
    assert g.call(ACME, "echo", '{"x": "no"}').error.startswith("invalid arguments")
    assert g.call(ACME, "echo", {"x": 1}).error == "rate limit exceeded"


def test_without_a_rate_limit_calls_are_unlimited():
    g = governed()
    assert all(g.call(ACME, "echo", {}).success for _ in range(200))


def test_the_limit_holds_under_concurrent_calls():
    g = governed(rate_limit=RateLimit(calls=50, per_seconds=3600), clock=Clock())
    results = []
    lock = threading.Lock()

    def worker():
        for _ in range(10):
            r = g.call(ACME, "echo", {})
            with lock:
                results.append(r.success)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 200 and sum(results) == 50


def test_two_governed_toolkits_on_one_toolkit_keep_separate_windows():
    shared = kit()
    a = governed(shared, rate_limit=RateLimit(calls=1, per_seconds=60), clock=Clock())
    b = governed(shared, rate_limit=RateLimit(calls=1, per_seconds=60), clock=Clock())
    assert a.call(ACME, "echo", {}).success and b.call(ACME, "echo", {}).success
    assert a.call(ACME, "echo", {}).error == "rate limit exceeded"


# -- approval ----------------------------------------------------------------------------------------------------------

def test_tools_that_need_no_approval_never_call_approve():
    calls = []
    g = governed(require_approval=["wipe"], approve=lambda s, t, a: calls.append(1) or True)
    assert g.call(ACME, "echo", {}).success
    assert calls == []


def test_an_approved_call_runs():
    ran = []
    g = governed(kit(tool("echo"), counting_wipe(ran, "done")),
                 require_approval=["wipe"], approve=lambda s, t, a: True)
    assert g.call(ACME, "wipe", {}) == ToolResult(success=True, result="done")
    assert ran == [1]


@pytest.mark.parametrize("verdict", [False, None, 0, 1, "yes", "True", [True], {"ok": True}, object()])
def test_only_exactly_true_approves(verdict):
    ran = []
    g = governed(kit(tool("echo"), counting_wipe(ran)),
                 require_approval=["wipe"], approve=lambda s, t, a: verdict)
    assert g.call(ACME, "wipe", {}) == ToolResult(success=False, error="approval denied")
    assert ran == []


def test_an_approve_hook_that_raises_denies_and_logs_only_the_type(caplog):
    ran = []

    def approve(scope, tool_, args):
        raise RuntimeError("CANARY-APPROVE internal detail")

    g = governed(kit(tool("echo"), counting_wipe(ran)),
                 require_approval=["wipe"], approve=approve)
    with caplog.at_level(logging.WARNING, logger="ragleap_tools"):
        r = g.call(ACME, "wipe", {})
    assert r == ToolResult(success=False, error="approval denied") and ran == []
    assert "RuntimeError" in caplog.text and "CANARY-APPROVE" not in caplog.text


def test_a_tool_that_needs_approval_is_refused_when_no_approve_was_given():
    ran = []
    events = []
    g = governed(kit(tool("echo"), counting_wipe(ran)),
                 require_approval=["wipe"], on_event=events.append)
    assert g.call(ACME, "wipe", {}) == ToolResult(success=False, error="approval required")
    assert ran == [] and events[0]["error"] == "approval_required" and events[0]["approval"] == "denied"


def test_approve_gets_the_scope_the_tool_and_the_validated_arguments():
    seen = []
    wipe = tool("wipe", props={"x": {"type": "integer"}, "y": {"type": "string"}})
    g = governed(kit(tool("echo"), wipe), require_approval=["wipe"],
                 approve=lambda s, t, a: seen.append((s, t, a)) or True)
    g.call(ACME, "wipe", '{"x": 1, "y": null}')
    scope, tool_, args = seen[0]
    assert scope is ACME and tool_ is wipe and args == {"x": 1}


def test_arguments_that_fail_validation_never_reach_approve():
    seen = []
    g = governed(require_approval=["wipe"], approve=lambda s, t, a: seen.append(1) or True)
    assert g.call(ACME, "wipe", '{"x": "no"}').error.startswith("invalid arguments")
    assert g.call(ACME, "wipe", "{not json").error == "arguments are not valid JSON"
    assert seen == []


def test_arguments_that_cannot_be_copied_for_approve_are_denied_and_never_raise(caplog):
    ran = []
    wipe = Tool(name="wipe", description="d", parameters={"type": "object", "properties": {"v": {}}},
                handler=lambda **kw: ran.append(1) or ToolResult(success=True, result=1))
    g = governed(kit(tool("echo"), wipe), require_approval=["wipe"], approve=lambda s, t, a: True)
    with caplog.at_level(logging.WARNING, logger="ragleap_tools"):
        r = g.call(ACME, "wipe", {"v": threading.Lock()})
    assert r == ToolResult(success=False, error="approval denied") and ran == []
    assert "TypeError" in caplog.text


def test_approve_cannot_change_what_runs_by_editing_its_copy_of_the_arguments():
    ran = []

    def handler(**kw):
        ran.append(kw)
        return ToolResult(success=True, result=kw)

    def approve(scope, tool_, args):
        args["x"] = 999
        args.setdefault("evil", []).append(1)
        return True

    g = governed(kit(tool("echo"), tool("wipe", handler=handler, props={"x": {"type": "integer"}})),
                 require_approval=["wipe"], approve=approve)
    assert g.call(ACME, "wipe", {"x": 1}).success
    assert ran == [{"x": 1}]


def test_approve_edits_to_nested_arguments_do_not_reach_the_handler_either():
    ran = []
    wipe = tool("wipe", handler=lambda **kw: ran.append(kw) or ToolResult(success=True, result=1),
                props={"items": {"type": "array", "items": {"type": "integer"}}})

    def approve(scope, tool_, args):
        args["items"].append(666)
        return True

    g = governed(kit(tool("echo"), wipe), require_approval=["wipe"], approve=approve)
    g.call(ACME, "wipe", {"items": [1, 2]})
    assert ran == [{"items": [1, 2]}]


# -- running and results ---------------------------------------------------------------------------------------------

def test_invalid_arguments_return_the_same_failure_toolkit_call_returns():
    g = governed()
    for args in ('{"x": "no"}', "{bad", "[1]", {"zzz": 1}, {1: "a"}):
        assert g.call(ACME, "echo", args) == kit().call("echo", args)


def test_handler_failures_pass_through_unchanged():
    t = [tool("fail", handler=lambda **kw: ToolResult(success=False, error="declined")),
         tool("err", handler=lambda **kw: (_ for _ in ()).throw(ToolError("not allowed"))),
         tool("boom", handler=lambda **kw: (_ for _ in ()).throw(RuntimeError("CANARY-EXC detail"))),
         tool("odd", handler=lambda **kw: "text")]
    g = GovernedToolkit(Toolkit(t), allow_for=lambda s: ["fail", "err", "boom", "odd"])
    assert g.call(ACME, "fail", {}) == ToolResult(success=False, error="declined")
    assert g.call(ACME, "err", {}) == ToolResult(success=False, error="not allowed")
    assert g.call(ACME, "boom", {}) == ToolResult(success=False, error="boom failed (RuntimeError)")
    assert g.call(ACME, "odd", {}) == ToolResult(success=False, error="odd returned an invalid result")


def test_null_optional_arguments_are_dropped_before_the_handler_like_toolkit_call_does():
    rec = []
    g = governed(kit(tool("echo", handler=lambda **kw: rec.append(kw) or ToolResult(success=True, result=kw))))
    g.call(ACME, "echo", '{"x": null}')
    assert rec == [{}]


def test_the_wrapped_toolkit_is_not_changed():
    shared = kit()
    before = (shared.names(), len(shared))
    g = governed(shared, rate_limit=RateLimit(calls=1, per_seconds=1), clock=Clock())
    for _ in range(3):
        g.call(ACME, "echo", {})
    assert (shared.names(), len(shared)) == before
    assert shared.call("echo", {"x": 1}).success is True


def test_with_everything_allowed_and_no_policy_it_matches_toolkit_call_for_known_tools():
    plain = Toolkit([tool("echo"), tool("req", props={"x": {"type": "integer"}}, required=["x"]),
                     tool("boom", handler=lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))])
    g = GovernedToolkit(plain, allow_for=lambda s: plain.names())
    inputs = [None, "", "{}", '{"x": 1}', '{"x": "1"}', '{"x": null}', "{bad", "[1]", 5,
              {"x": 2}, {"zzz": 1}, {1: 2}, b"{}", ("x",)]
    for name in ("echo", "req", "boom"):
        for args in inputs:
            assert g.call(ACME, name, args) == plain.call(name, args), (name, args)


# -- events ----------------------------------------------------------------------------------------------------------

def test_every_call_attempt_produces_exactly_one_event_with_these_fields():
    events = []
    g = governed(require_approval=["wipe"], approve=lambda s, t, a: True, rate_limit=RateLimit(calls=1, per_seconds=60),
                 on_event=events.append, clock=Clock())
    g.call(ACME, "echo", {"x": 1})
    g.call(ACME, "echo", {"x": 1})
    g.call("bad", "echo", {})
    g.call(OTHER, "nope", {})
    assert len(events) == 4
    for e in events:
        assert e["event"] == "call" and isinstance(e["duration_ms"], int) and e["duration_ms"] >= 0
    assert set(events[0]) == {"event", "tool", "ok", "error", "approval", "duration_ms", "tenant", "scope"}
    assert set(events[2]) == {"event", "tool", "ok", "error", "approval", "duration_ms"}


def test_event_values_for_each_outcome():
    events = []
    clock = Clock()
    g = GovernedToolkit(
        kit(tool("echo"), tool("wipe"), tool("fail", handler=lambda **kw: ToolResult(success=False, error="declined"))),
        allow_for=lambda s: ["echo", "wipe", "fail"], require_approval=["wipe"],
        approve=lambda s, t, a: s.subject == "alice",
        rate_limit=RateLimit(calls=5, per_seconds=60), on_event=events.append, clock=clock)
    g.call(ACME, "echo", {})
    g.call(ACME, "echo", '{"x": "no"}')
    g.call(ACME, "wipe", {})
    g.call(Scope(tenant="acme", subject="mallory"), "wipe", {})
    g.call(ACME, "fail", {})
    pick = lambda e: (e["tool"], e["ok"], e["error"], e["approval"])
    assert [pick(e) for e in events] == [
        ("echo", True, None, "not_required"),
        ("echo", False, "invalid_arguments", "unchecked"),
        ("wipe", True, None, "approved"),
        ("wipe", False, "approval_denied", "denied"),
        ("fail", False, "tool_failed", "not_required"),
    ]
    assert events[0]["tenant"] == "acme" and events[0]["scope"] == "tenant=acme/subject=alice"
    assert events[3]["scope"] == "tenant=acme/subject=mallory"
    g.call(ACME, "echo", {})
    assert pick(events[5]) == ("echo", False, "rate_limited", "unchecked")


def test_a_failing_event_hook_never_breaks_the_call_and_logs_only_the_type(caplog):
    def hook(event):
        raise RuntimeError("CANARY-HOOK detail")

    g = governed(on_event=hook)
    with caplog.at_level(logging.WARNING, logger="ragleap_tools"):
        r = g.call(ACME, "echo", {"x": 1})
    assert r == ToolResult(success=True, result={"x": 1})
    assert "RuntimeError" in caplog.text and "CANARY-HOOK" not in caplog.text


def test_the_event_is_a_fresh_dict_each_time():
    events = []
    g = governed(on_event=events.append)
    g.call(ACME, "echo", {})
    events[0]["tool"] = "tampered"
    g.call(ACME, "echo", {})
    assert events[1]["tool"] == "echo"


def test_no_hook_means_no_event_work_and_nothing_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="ragleap_tools"):
        assert governed().call(ACME, "echo", {}).success is True
    assert caplog.records == []


def test_canary_strings_never_reach_an_event_or_a_refusal():
    events = []

    def handler(**kw):
        return ToolResult(success=True, result="CANARY-RESULT")

    def bad(**kw):
        raise RuntimeError("CANARY-EXC")

    def approve(scope, tool_, args):
        raise RuntimeError("CANARY-APPROVE")

    def allow(scope):
        if scope.tenant == "broken":
            raise RuntimeError("CANARY-ALLOW")
        return ["echo", "bad", "wipe"]

    g = GovernedToolkit(Toolkit([tool("echo", handler=handler, props={"note": {"type": "string"}}),
                                 tool("bad", handler=bad, props={"note": {"type": "string"}}),
                                 tool("wipe", handler=handler, props={"note": {"type": "string"}})]),
                        allow_for=allow, require_approval=["wipe"], approve=approve, on_event=events.append)
    arg = {"note": "CANARY-ARG"}
    results = [
        g.call(ACME, "echo", arg),
        g.call(ACME, "bad", arg),
        g.call(ACME, "wipe", arg),
        g.call(Scope(tenant="broken"), "echo", arg),
        g.call(ACME, "echo", '{"note": 5, "x": "CANARY-ARG"}'),
        g.call(ACME, "CANARY-ARG", arg),
    ]
    assert len(events) == 6
    assert "CANARY" not in repr(events)
    assert all("CANARY" not in (r.error or "") for r in results)
    assert results[0].result == "CANARY-RESULT"


def test_call_never_raises_for_odd_input():
    g = governed(require_approval=["wipe"], approve=lambda s, t, a: True,
                 rate_limit=RateLimit(calls=1000, per_seconds=1), clock=Clock())
    odd = [None, "", 0, 5, 1.5, True, b"x", [], {}, (), object(), "echo", ["echo"], {"echo": 1}]
    for scope in (ACME, None, "acme", 5, object()):
        for name in odd:
            for args in odd:
                r = g.call(scope, name, args)
                assert isinstance(r, ToolResult)


def test_governed_toolkit_is_exported_from_the_package():
    import ragleap_tools

    assert ragleap_tools.GovernedToolkit is GovernedToolkit
    assert "GovernedToolkit" in ragleap_tools.__all__
