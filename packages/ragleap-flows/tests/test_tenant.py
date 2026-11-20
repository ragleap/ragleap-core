"""Tenant isolation, the way a hostile tenant would try to break it. Positive controls keep every test honest."""
import json
import re
import threading

import pytest
from ragleap_agents import InMemoryStateStore, ResumeError, SQLiteStateStore
from ragleap_security import AuditLog, Scope, ScopeError

import ragleap_flows as rf
from conftest import A, B, make, refund_flow

PAUSE = {"amount": 500, "who": "ann@x.test"}


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return InMemoryStateStore() if request.param == "memory" else SQLiteStateStore(tmp_path / "s.db")


def test_a_run_needs_exactly_a_scope(kit):
    rn = rf.FlowRunner(refund_flow(), kit)
    import types

    lookalike = types.SimpleNamespace(tenant="acme", subject=None, agent=None, session=None, key="tenant=acme")
    for bad in (None, "acme", {"tenant": "acme"}, ("acme",), 5, lookalike):
        with pytest.raises(Exception):
            rn.run(bad, PAUSE)
        with pytest.raises(Exception):
            rn.resume(bad, "x" * 22, {"ask": True})
    assert rn.run(A, PAUSE).status == "awaiting_approval"


def test_another_tenant_cannot_resume_or_see_a_paused_run(kit, calls, store):
    rn = rf.FlowRunner(refund_flow(), kit, store=store)
    r = rn.run(A, PAUSE)
    with pytest.raises(ResumeError) as foreign:
        rn.resume(B, r.run_id, {"ask": True})
    with pytest.raises(ResumeError) as unknown:
        rn.resume(B, "no-such-run-id-0000000000", {"ask": True})
    with pytest.raises(ResumeError) as unknown_for_owner:
        rn.resume(A, "no-such-run-id-0000000000", {"ask": True})
    assert str(foreign.value) == str(unknown.value) == str(unknown_for_owner.value) == "unknown run"
    assert not [c for c in calls.log if c[0] == "send"]                        # the tool never ran for the attacker
    assert rn.resume(A, r.run_id, {"ask": True}).status == "done"             # positive control: A is unaffected


@pytest.mark.parametrize("other", [Scope("acme", "alice"), Scope("acme", agent="bot"), Scope("acme2"), Scope("ACME"), Scope("acm")])
def test_every_part_of_the_scope_must_match(kit, store, other):
    rn = rf.FlowRunner(refund_flow(), kit, store=store)
    mine = Scope("acme", "bob")
    r = rn.run(mine, PAUSE)
    with pytest.raises(ResumeError, match="unknown run"):
        rn.resume(other, r.run_id, {"ask": True})
    with pytest.raises(ResumeError, match="unknown run"):
        rn.resume(Scope("acme"), r.run_id, {"ask": True})                      # a wider scope is refused too
    assert rn.resume(mine, r.run_id, {"ask": True}).status == "done"


def test_the_ownership_check_comes_before_the_decision_is_read(kit):
    rn = rf.FlowRunner(refund_flow(), kit)
    r = rn.run(A, PAUSE)
    for decisions in ({}, {"ask": "x"}, {"zzz": True}, None):
        with pytest.raises(ResumeError) as e:
            rn.resume(B, r.run_id, decisions)
        assert str(e.value) == "unknown run"                                   # never "no decision": that would confirm the run


def test_a_damaged_stored_state_gives_the_same_error(kit):
    st = InMemoryStateStore()
    rn = rf.FlowRunner(refund_flow(), kit, store=st)
    r = rn.run(A, PAUSE)
    good = st.load(r.run_id)
    for damage in ({"pending": {"nope": 1}}, {"pending": "x"}, {"scope": None}, {"scope": 5}, {"pending": {"step": "ghost"}}):
        st.save(r.run_id, {**good, **damage})
        with pytest.raises(ResumeError) as e:
            rn.resume(A, r.run_id, {"ask": True})
        assert str(e.value) in ("unknown run",)
    st.save(r.run_id, good)
    assert rn.resume(A, r.run_id, {"ask": True}).status == "done"


def test_a_corrupt_sqlite_row_is_refused_not_run(kit, tmp_path):
    import sqlite3

    st = SQLiteStateStore(tmp_path / "s.db")
    rn = rf.FlowRunner(refund_flow(), kit, store=st)
    r = rn.run(A, PAUSE)
    db = sqlite3.connect(tmp_path / "s.db")
    db.execute("UPDATE runs SET state = 'not json'")
    db.commit()
    with pytest.raises(ResumeError, match="unknown run"):
        rn.resume(A, r.run_id, {"ask": True})


def test_a_run_of_another_flow_version_is_refused_for_its_owner_only(kit, store):
    old = rf.FlowRunner(refund_flow(), kit, store=store)
    r = old.run(A, PAUSE)
    changed = make([{"id": "ask", "type": "approval", "prompt": "Approve?"}])
    new = rf.FlowRunner(changed, kit, store=store)
    with pytest.raises(ResumeError, match="different version"):
        new.resume(A, r.run_id, {"ask": True})
    with pytest.raises(ResumeError, match="unknown run"):
        new.resume(B, r.run_id, {"ask": True})                                  # a stranger learns nothing about versions


def test_run_ids_are_128_bit_random_tokens(kit):
    rn = rf.FlowRunner(refund_flow(), kit)
    ids = {rn.run(A, PAUSE).run_id for _ in range(200)}
    assert len(ids) == 200 and all(re.fullmatch(r"[A-Za-z0-9_-]{22}", i) for i in ids)


def test_saved_state_holds_the_scope_and_never_another_tenants_data(kit, store):
    rn = rf.FlowRunner(refund_flow(), kit, store=store)
    ra = rn.run(A, {"amount": 500, "who": "alice-secret@a.test"})
    rb = rn.run(B, {"amount": 500, "who": "bob-secret@b.test"})
    sa, sb = store.load(ra.run_id), store.load(rb.run_id)
    assert sa["scope"] == "tenant=acme" and sb["scope"] == "tenant=globex"
    assert "bob-secret" not in json.dumps(sa) and "alice-secret" not in json.dumps(sb)


def test_one_store_per_tenant_keeps_runs_apart_even_with_a_wrong_scope(kit, tmp_path):
    stores = {}
    rn = rf.FlowRunner(refund_flow(), kit, store_for=lambda t: stores.setdefault(t, InMemoryStateStore()))
    r = rn.run(A, PAUSE)
    assert set(stores) == {"acme"} and stores["acme"].load(r.run_id) is not None
    with pytest.raises(ResumeError, match="unknown run"):
        rn.resume(B, r.run_id, {"ask": True})
    assert stores["globex"].load(r.run_id) is None                              # the other tenant's store never saw it
    assert rn.resume(A, r.run_id, {"ask": True}).status == "done"


def test_store_for_must_return_a_store(kit):
    rn = rf.FlowRunner(refund_flow(), kit, store_for=lambda t: object())
    with pytest.raises(rf.FlowError):
        rn.run(A, PAUSE)


def test_hostile_inputs_cannot_reach_attributes_or_other_tenants(kit, calls):
    f = make([{"id": "a", "type": "tool", "tool": "echo", "arguments": {"value": "$input.__class__"}}])
    r = rf.FlowRunner(f, kit).run(A, {"x": 1})
    assert r.status == "failed" and r.stop_reason == "missing_value"
    f2 = make([{"id": "a", "type": "tool", "tool": "echo", "arguments": {"value": "$input.k"}}])
    r2 = rf.FlowRunner(f2, kit).run(A, {"k": "$input.secret", "secret": "TOP"})   # data that looks like a path is data
    assert r2.status == "done" and r2.output is None and ("echo", {"value": "$input.secret"}) in calls.log


def test_concurrent_runs_for_many_tenants_never_mix(kit, tmp_path):
    rn = rf.FlowRunner(refund_flow(), kit, store=SQLiteStateStore(tmp_path / "s.db"))
    tenants = [Scope("t%d" % i) for i in range(6)]
    paused, errors = {}, []

    def work(sc):
        try:
            for n in range(5):
                r = rn.run(sc, {"amount": 500 + n, "who": "u-%s" % sc.tenant})
                paused[r.run_id] = sc
                other = tenants[(tenants.index(sc) + 1) % len(tenants)]
                try:
                    rn.resume(other, r.run_id, {"ask": True})
                    errors.append("crossed")
                except ResumeError:
                    pass
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=work, args=(sc,)) for sc in tenants]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == [] and len(paused) == 30
    for rid, sc in paused.items():
        assert rn.resume(sc, rid, {"ask": True}).status == "done"


def test_a_tainted_run_forces_approval_before_every_outbound_step(kit, calls):
    f = make([{"id": "s1", "type": "tool", "tool": "send", "outbound": True, "arguments": {"to": "a"}, "next": "s2"},
              {"id": "s2", "type": "tool", "tool": "send", "outbound": True, "arguments": {"to": "b"}}])
    rn = rf.FlowRunner(f, kit)
    r = rn.run(A, {}, tainted=True)
    assert r.status == "awaiting_approval" and r.pending["forced"] is True and r.pending["step"] == "s1" and calls.log == []
    r = rn.resume(A, r.run_id, {"s1": True})
    assert r.status == "awaiting_approval" and r.pending["step"] == "s2" and len(calls.log) == 1   # each step asks again
    assert rn.resume(A, r.run_id, {"s2": True}).status == "done" and len(calls.log) == 2
    clean = rn.run(A, {})                                                       # positive control: not tainted, no pause
    assert clean.status == "done"


def test_an_external_step_taints_the_rest_of_the_run(kit, calls):
    f = make([{"id": "get", "type": "tool", "tool": "fetch", "external": True, "next": "out"},
              {"id": "out", "type": "tool", "tool": "send", "outbound": True, "arguments": {"to": "a"}}])
    r = rf.FlowRunner(f, kit).run(A, {})
    assert r.status == "awaiting_approval" and r.tainted is True and r.pending["forced"] is True
    f2 = make([{"id": "get", "type": "tool", "tool": "fetch", "next": "out"},
               {"id": "out", "type": "tool", "tool": "send", "outbound": True, "arguments": {"to": "a"}}])
    assert rf.FlowRunner(f2, kit).run(A, {}).status == "done"                   # not declared external: no taint


def test_a_forced_rejection_stops_before_the_outbound_tool(kit, calls):
    f = make([{"id": "s1", "type": "tool", "tool": "send", "outbound": True, "arguments": {"to": "a"}}])
    rn = rf.FlowRunner(f, kit)
    r = rn.run(A, {}, tainted=True)
    assert rn.resume(A, r.run_id, {"s1": False}).status == "rejected" and calls.log == []


class TestAudit:
    def test_events_hold_ids_and_sizes_never_text(self, kit, tmp_path):
        log = AuditLog(tmp_path / "a.jsonl")
        rn = rf.FlowRunner(refund_flow(), kit, audit=log)
        r = rn.run(A, {"amount": 500, "who": "ann-secret@x.test"})
        rn.resume(A, r.run_id, {"ask": True})
        raw = open(log.path).read()
        assert "ann-secret" not in raw and "Approve this refund" not in raw
        events = [json.loads(l) for l in raw.splitlines()]
        names = [e["event"] for e in events]
        assert names == ["flow.start", "step.executed", "approval.requested", "approval.decided", "step.executed", "flow.end"]
        assert all(e["scope"] == "tenant=acme" for e in events) and log.verify().ok

    def test_if_the_record_cannot_be_written_nothing_continues(self, kit, calls):
        class Broken(AuditLog):
            def __init__(self):
                pass

            def append(self, *a, **k):
                raise OSError("disk full")

        rn = rf.FlowRunner(refund_flow(), kit, audit=Broken())
        with pytest.raises(OSError):
            rn.run(A, {"amount": 1, "who": "x"})
        assert calls.log == []
