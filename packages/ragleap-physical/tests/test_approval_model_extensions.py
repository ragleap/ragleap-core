"""Issue #1337-#1339 desired-behaviour tests and explicit open-gap markers."""
"""Issue #1337-#1339: identity and stop semantics. Wanted-behaviour tests and explicit open-gap markers."""
import importlib.util
from pathlib import Path

import pytest

_model_path = Path(__file__).resolve().parents[1] / "benchmarks" / "approval_model.py"
_spec = importlib.util.spec_from_file_location("approval_model_ext", _model_path)
approval_model = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(approval_model)
Approval = approval_model.Approval
StopModel = approval_model.StopModel
identity_key = approval_model.identity_key
model_event = approval_model.model_event
p6_violation = approval_model.p6_violation
p7_violation = approval_model.p7_violation
p8_self_approval = approval_model.p8_self_approval
replay_multi_proposal = approval_model.replay_multi_proposal

from ragleap_physical import ApprovalError, SafetyStop
from helpers import make, propose


def test_p8_identity_is_case_and_compatibility_form_insensitive():
    assert p8_self_approval("employee:ops", "EMPLOYEE:OPS")
    assert p8_self_approval("employee:ops", "ｅｍｐｌｏｙｅｅ:ｏｐｓ")
    assert identity_key("Straße") == identity_key("STRASSE")


def test_p8_deliberate_break_proof_mutates_model_identity_key(monkeypatch):
    # Break the model's own canonicalizer, not a parallel lambda comparator.
    monkeypatch.setattr(approval_model, "identity_key", lambda value: value)
    assert not approval_model.p8_self_approval("employee:ops", "EMPLOYEE:OPS")
    with pytest.raises(AssertionError):
        assert approval_model.p8_self_approval("employee:ops", "EMPLOYEE:OPS")


def test_p6_stop_invalidates_outstanding_approvals_model_and_mutation_proof(monkeypatch):
    approval = Approval("p1", "employee:ops", "person:alice", "digest")
    state = model_event(StopModel(), "approve", approval)
    assert state.approvals[0].valid
    stopped = model_event(state, "stop")
    assert not stopped.approvals[0].valid
    assert not p6_violation(stopped)

    # Deliberately break model_event's stop transition so it preserves approvals.
    original = approval_model.model_event
    def broken_model_event(current, event, approval=None):
        if event == "stop":
            return StopModel(stopped=True, approvals=current.approvals,
                             command_after_stop=current.command_after_stop)
        return original(current, event, approval)
    monkeypatch.setattr(approval_model, "model_event", broken_model_event)
    broken = approval_model.model_event(state, "stop")
    assert p6_violation(broken), "P6 detector must catch approval surviving stop"


def test_p7_model_exposes_stop_at_adapter_call_boundary_and_mutation_proof(monkeypatch):
    state = model_event(StopModel(), "stop_after_recheck_before_adapter")
    state = model_event(state, "adapter_command")
    assert p7_violation(state)

    # Mutate the model's own event transition to omit the adapter-after-stop mark.
    original = approval_model.model_event
    def broken_model_event(current, event, approval=None):
        if event == "adapter_command":
            return StopModel(stopped=current.stopped, approvals=current.approvals,
                             command_after_stop=current.command_after_stop)
        return original(current, event, approval)
    monkeypatch.setattr(approval_model, "model_event", broken_model_event)
    broken = approval_model.model_event(
        approval_model.model_event(StopModel(), "stop_after_recheck_before_adapter"),
        "adapter_command",
    )
    with pytest.raises(AssertionError):
        assert p7_violation(broken)


class StopDuringDryRunAdapter:
    """Inject a stop synchronously from dry_run to exercise the re-check."""
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.bridge = None
        self.command_seen = False
    def __getattr__(self, name):
        return getattr(self.wrapped, name)
    @property
    def simulated(self): return True
    def targets(self): return self.wrapped.targets()
    def observe_raw(self): return self.wrapped.observe_raw()
    def dry_run(self, *args, **kwargs):
        self.bridge.stop()
        return self.wrapped.dry_run(*args, **kwargs)
    def apply(self, *args, **kwargs):
        self.command_seen = True
        return self.wrapped.apply(*args, **kwargs)
    def stop(self): return self.wrapped.stop()
    def clear_stop(self): return self.wrapped.clear_stop()
    def advance(self, *args, **kwargs): return self.wrapped.advance(*args, **kwargs)


def test_p7_bridge_rechecks_stop_after_dry_run_before_adapter_command():
    from ragleap_physical.adapters import FakeJointAdapter
    wrapped = FakeJointAdapter()
    hooked = StopDuringDryRunAdapter(wrapped)
    bridge, _adapter, _clock = make(adapter=hooked)
    hooked.bridge = bridge
    proposal = propose(bridge)
    bridge.approve(proposal, "person:alice")
    with pytest.raises(SafetyStop):
        bridge.apply(proposal)
    assert hooked.command_seen is False
    assert wrapped.applied == []


class StopAtAdapterCallAdapter(StopDuringDryRunAdapter):
    """Inject stop after the final Bridge re-check, at adapter-call boundary."""
    def dry_run(self, *args, **kwargs):
        return self.wrapped.dry_run(*args, **kwargs)
    def apply(self, *args, **kwargs):
        self.bridge.stop()
        self.command_seen = True
        return self.wrapped.apply(*args, **kwargs)


def test_p7_a_stopped_adapter_does_not_move_when_stop_lands_at_the_adapter_call():
    """The Bridge cannot stop a call it has already made; the adapter must refuse a command while stopped.
    The simulated stand-ins refuse a command while stopped (issue 1339)."""
    from ragleap_physical.adapters import FakeJointAdapter
    wrapped = FakeJointAdapter()
    hooked = StopAtAdapterCallAdapter(wrapped)
    bridge, _adapter, _clock = make(adapter=hooked)
    hooked.bridge = bridge
    proposal = propose(bridge)
    bridge.approve(proposal, "person:alice")
    try:
        bridge.apply(proposal)
    except Exception:
        pass  # a refusing adapter makes apply fail; the Bridge's answer is not what this test checks
    assert wrapped.applied == []


def test_p8_bridge_rejects_case_variant_self_approval():
    bridge, _adapter, _clock = make()
    proposal = propose(bridge)
    with pytest.raises(ApprovalError, match="own proposal"):
        bridge.approve(proposal, "EMPLOYEE:OPS")


def test_multi_proposal_replay_has_no_p8_disagreement():
    findings = replay_multi_proposal(make, propose)
    assert [f for f in findings if f["sequence"].startswith("P8-")] == []


def test_multi_proposal_replay_has_no_p6_disagreement():
    findings = replay_multi_proposal(make, propose)
    assert [f for f in findings if f["sequence"].startswith("P6-")] == []


def test_p8_non_ascii_compatibility_form_is_rejected_by_bridge_actor_grammar():
    bridge, _adapter, _clock = make()
    proposal = propose(bridge)
    with pytest.raises(ApprovalError, match="named person id"):
        bridge.approve(proposal, "ｅｍｐｌｏｙｅｅ:ｏｐｓ")
