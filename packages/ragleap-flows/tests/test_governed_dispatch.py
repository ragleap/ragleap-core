"""Flows with a real GovernedToolkit (ragleap-tools 0.6.0 or newer) as the dispatcher.

The owner's per-tenant allowlist, approval hook and rate limit apply to every tool step of a flow, and the
governed events carry no flow data. These tests use the released GovernedToolkit, not a stand-in."""
import pytest
from ragleap_security import Scope

import ragleap_flows as rf
from conftest import A, B, refund_flow

ragleap_tools = pytest.importorskip("ragleap_tools")
if not hasattr(ragleap_tools, "GovernedToolkit"):
    pytest.skip("needs ragleap-tools 0.6.0 or newer (GovernedToolkit)", allow_module_level=True)
from ragleap_tools import GovernedToolkit  # noqa: E402
from ragleap_tools.governed import RateLimit  # noqa: E402

SMALL = {"amount": 50, "who": "ann@x.test"}      # below 100: the flow skips its own approval step and sends
BIG = {"amount": 500, "who": "ann@x.test"}


def sends(calls, to=None):
    return [c for c in calls.log if c[0] == "send" and (to is None or c[1].get("to") == to)]


def test_dispatch_gets_the_exact_scope_of_the_run(kit, calls):
    seen = []

    def allow_for(scope):
        seen.append(scope)
        return ["echo", "send"]

    gov = GovernedToolkit(kit, allow_for=allow_for)
    me = Scope("acme", "bob")
    res = rf.FlowRunner(refund_flow(), dispatch=gov.call).run(me, SMALL)
    assert res.status == "done"
    assert len(seen) == 2 and all(s == me and s.key == "tenant=acme/subject=bob" for s in seen)   # echo, then send


def test_a_tenants_allowlist_decides_which_flow_steps_can_run(kit, calls):
    gov = GovernedToolkit(kit, allow_for=lambda s: ["echo", "send"] if s.tenant == "acme" else ["echo"])
    rn = rf.FlowRunner(refund_flow(), dispatch=gov.call)
    ok = rn.run(A, SMALL)
    assert ok.status == "done" and len(sends(calls)) == 1
    blocked = rn.run(B, {"amount": 50, "who": "mallory@x.test"})
    assert blocked.status == "failed" and blocked.stop_reason == "tool_failed:pay"
    assert not sends(calls, "mallory@x.test")                                  # the tool never ran for the other tenant
    assert [s["status"] for s in blocked.steps] == ["ok", "ok", "failed"]      # echo and the branch ran, send was refused


def test_an_allowlist_that_fails_denies_every_step(kit, calls):
    def broken(scope):
        raise RuntimeError("policy store down")

    res = rf.FlowRunner(refund_flow(), dispatch=GovernedToolkit(kit, allow_for=broken).call).run(A, SMALL)
    assert res.status == "failed" and res.stop_reason == "tool_failed:look"    # deny by default, at the first tool step
    assert not calls.log


def test_the_owners_approval_hook_is_separate_from_the_flows_own_approval(kit, calls):
    asked = []

    def approve(scope, tool, arguments):
        asked.append((scope.tenant, tool.name, dict(arguments)))
        return arguments.get("to") == "ann@x.test"

    gov = GovernedToolkit(kit, allow_for=lambda s: ["echo", "send"], require_approval=["send"], approve=approve)
    rn = rf.FlowRunner(refund_flow(), dispatch=gov.call)
    paused = rn.run(A, BIG)
    assert paused.status == "awaiting_approval" and not asked                  # the flow's own checkpoint comes first
    assert rn.resume(A, paused.run_id, {"ask": True}).status == "done"        # then the owner's hook says yes
    assert asked == [("acme", "send", {"to": "ann@x.test", "body": 500})] and len(sends(calls)) == 1
    refused = rn.run(A, {"amount": 50, "who": "eve@x.test"})                   # the hook says no for this address
    assert refused.status == "failed" and refused.stop_reason == "tool_failed:pay"
    assert not sends(calls, "eve@x.test")


def test_a_flow_approval_does_not_replace_the_owners_approval(kit, calls):
    gov = GovernedToolkit(kit, allow_for=lambda s: ["echo", "send"], require_approval=["send"])   # no approve hook
    rn = rf.FlowRunner(refund_flow(), dispatch=gov.call)
    paused = rn.run(A, BIG)
    done_or_not = rn.resume(A, paused.run_id, {"ask": True})
    assert done_or_not.status == "failed" and done_or_not.stop_reason == "tool_failed:pay"
    assert not sends(calls)


def test_the_rate_limit_is_per_tenant_across_runs(kit, calls):
    gov = GovernedToolkit(kit, allow_for=lambda s: ["echo", "send"], rate_limit=RateLimit(calls=3, per_seconds=60),
                          clock=lambda: 0.0)
    rn = rf.FlowRunner(refund_flow(), dispatch=gov.call)
    assert rn.run(A, SMALL).status == "done"                                   # 2 calls used
    second = rn.run(A, SMALL)                                                   # echo is the 3rd call, send is refused
    assert second.status == "failed" and second.stop_reason == "tool_failed:pay"
    assert rn.run(B, SMALL).status == "done"                                    # another tenant has its own window
    assert len(sends(calls)) == 2


def test_governed_events_carry_the_scope_and_no_flow_data(kit, calls):
    events = []
    gov = GovernedToolkit(kit, allow_for=lambda s: ["echo", "send"], on_event=events.append)
    res = rf.FlowRunner(refund_flow(), dispatch=gov.call).run(Scope("acme", "bob"), SMALL)
    assert res.status == "done"
    assert [(e["tool"], e["ok"], e["tenant"], e["scope"]) for e in events] == [
        ("echo", True, "acme", "tenant=acme/subject=bob"), ("send", True, "acme", "tenant=acme/subject=bob")]
    for e in events:
        assert set(e) == {"event", "tool", "ok", "error", "approval", "duration_ms", "tenant", "scope"}   # no arguments, no results
    assert "ann@x.test" not in repr(events)
