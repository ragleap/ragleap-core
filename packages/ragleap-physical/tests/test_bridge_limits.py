"""Threads, stop during a running command, and memory caps."""
import threading

import pytest

from ragleap_physical import ApprovalError, Bridge, Capability, Envelope, EnvelopeViolation, SafetyStop
from ragleap_physical.adapters import FakeJointAdapter

from helpers import BASE, Clock, make, outcome, propose


def approved(bridge, **kw):
    p = propose(bridge, **kw)
    bridge.approve(p, "person:alice")
    return p


# ---- stop while a command is running
class Slow(FakeJointAdapter):
    """Pauses inside advance, like a long simulation slice, until the test lets it go."""

    def __init__(self):
        super().__init__()
        self.in_advance, self.proceed, self.advance_calls = threading.Event(), threading.Event(), 0

    def advance(self, to_sim_time_s, max_steps):
        self.advance_calls += 1
        if self.advance_calls == 1:
            self.in_advance.set()
            assert self.proceed.wait(10)
        return super().advance(to_sim_time_s, max_steps)


def test_stop_during_apply_returns_at_once_and_halts_after_the_current_slice():
    adapter = Slow()
    b, _, _ = make(adapter=adapter)
    p = approved(b)
    outcome_box = []

    def run():
        try:
            b.apply(p)
        except SafetyStop as exc:
            outcome_box.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert adapter.in_advance.wait(10)
    stopper = threading.Thread(target=b.stop)
    stopper.start()
    stopper.join(3)
    assert not stopper.is_alive(), "stop() must not wait for the running command"
    adapter.proceed.set()
    worker.join(10)
    assert outcome_box and adapter.stopped
    assert adapter.t <= 0.25 + 1e-9, "the command must not run past the slice that was already under way"
    assert adapter.advance_calls == 1
    assert [e["event"] for e in b.audit if e["event"] in ("rejected", "safe_stop")][-2:] == ["rejected", "safe_stop"]


def test_stop_flag_set_while_another_call_runs_is_enforced_when_it_finishes():
    adapter = FakeJointAdapter()
    b, _, _ = make(adapter=adapter)
    with b._lock:  # pretend a call is running in another thread
        t = threading.Thread(target=b.stop)
        t.start()
        t.join(3)
        assert not t.is_alive() and not adapter.stopped
    b.observe()  # the next call finishes and enforces the stop
    assert adapter.stopped


def test_stop_does_not_depend_on_capability():
    b, adapter, _ = make(level=Capability.OBSERVE)
    b.stop()
    assert adapter.stopped


def test_adapter_that_does_not_advance_is_caught_not_looped_forever():
    class Stuck(FakeJointAdapter):
        calls = 0

        def advance(self, to_sim_time_s, max_steps):
            Stuck.calls += 1
            assert Stuck.calls < 50, "the bridge is looping forever on an adapter that does not advance"
            return False
    b, adapter, _ = make(adapter=Stuck())
    p = approved(b)
    with pytest.raises(EnvelopeViolation) as e:
        b.apply(p)
    assert e.value.code == "no_progress" and adapter.stopped


# ---- threads
def test_many_threads_proposing_get_unique_ids_and_a_consistent_ledger():
    b = Bridge(FakeJointAdapter(), Envelope(BASE), org_id="org_demo", max_capability=Capability.CONTROL,
               clock=Clock(), max_pending=1000)
    ids, errors = [], []

    def work():
        try:
            for _ in range(25):
                ids.append(propose(b).proposal_id)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors and len(ids) == 200 and len(set(ids)) == 200
    assert len(b._ledger) == 200


# ---- memory caps
def test_pending_limit_stops_a_proposal_flood_and_recovers_after_expiry():
    b, _, clock = make(ttl_s=10.0)
    b2 = Bridge(FakeJointAdapter(), Envelope(BASE), org_id="org_demo", max_capability=Capability.CONTROL,
                clock=clock, ttl_s=10.0, max_pending=3)
    for _ in range(3):
        propose(b2)
    with pytest.raises(EnvelopeViolation) as e:
        propose(b2)
    assert e.value.code == "pending_limit"
    clock.t += 11.0
    propose(b2)  # expired proposals no longer count


def test_applying_a_proposal_frees_its_pending_slot():
    clock = Clock()
    b = Bridge(FakeJointAdapter(), Envelope(BASE), org_id="org_demo", max_capability=Capability.CONTROL,
               clock=clock, max_pending=1)
    p = approved(b)
    with pytest.raises(EnvelopeViolation):
        propose(b)
    b.apply(p)
    propose(b, value=0.2, expected_outcome=outcome(0.0, 1.0, by=b.observe().sim_time_s + 1.0))


def test_old_records_are_evicted_but_a_replay_is_still_refused():
    clock = Clock()
    b = Bridge(FakeJointAdapter(), Envelope(BASE), org_id="org_demo", max_capability=Capability.CONTROL,
               clock=clock, max_records=3, max_pending=100)
    first = approved(b)
    b.apply(first)
    for _ in range(5):
        propose(b)
    assert len(b._ledger) <= 6 and first.proposal_id not in b._ledger
    with pytest.raises(ApprovalError):
        b.apply(first)


def test_live_proposals_are_never_evicted():
    b = Bridge(FakeJointAdapter(), Envelope(BASE), org_id="org_demo", max_capability=Capability.CONTROL,
               clock=Clock(), max_records=2, max_pending=100)
    waiting = [propose(b) for _ in range(6)]
    assert all(p.proposal_id in b._ledger for p in waiting)


def test_audit_list_is_capped_but_the_sink_gets_everything():
    seen = []
    b = Bridge(FakeJointAdapter(), Envelope(BASE), org_id="org_demo", max_capability=Capability.CONTROL,
               clock=Clock(), max_audit=5, max_pending=1000, audit_sink=seen.append)
    for _ in range(20):
        propose(b)
    assert len(b.audit) == 5 and len(seen) == 20
