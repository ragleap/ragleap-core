"""Real-Bridge conformance checks for the approval-flow abstract model."""
import dataclasses
import importlib.util
from pathlib import Path

import pytest

_model_path = Path(__file__).resolve().parents[1] / "benchmarks" / "approval_model.py"
_spec = importlib.util.spec_from_file_location("approval_model", _model_path)
_model = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_model)
State = _model.State
transition = _model.transition
violations = _model.violations
explore = _model.explore

from ragleap_physical import ApprovalError, SafetyStop
from helpers import make, propose


def _approved():
    bridge, adapter, clock = make()
    proposal = propose(bridge)
    bridge.approve(proposal, "person:alice")
    return bridge, adapter, clock, proposal


def test_model_checker_explores_safe_graph_and_finds_deliberate_reuse_break():
    states, failure = explore()
    assert len(states) > 1
    assert failure is None
    mutated_states, mutated_failure = explore(allow_reuse=True)
    assert mutated_failure is not None
    trace, properties, state = mutated_failure
    assert "P1 proposal applied at most once" in properties
    assert state.applied_count == 2
    assert any(event == "reuse" for event, accepted in trace)


def test_propose_transition_matches_real_bridge():
    model = State()
    predicted = transition(model, "propose")
    bridge, _, _ = make()
    proposal = propose(bridge)
    assert predicted.accepted is True
    assert proposal.proposal_id == "prop_000001"
    assert predicted.state.proposed is True


def test_requester_self_approval_transition_matches_real_bridge():
    model = transition(State(), "propose").state
    predicted = transition(model, "approve_requester")
    bridge, _, _ = make()
    proposal = propose(bridge)
    with pytest.raises(ApprovalError):
        bridge.approve(proposal, proposal.requested_by)
    assert predicted.accepted is False


def test_second_approver_transition_matches_real_bridge():
    model = transition(State(), "propose").state
    predicted = transition(model, "approve_second")
    bridge, _, _ = make()
    proposal = propose(bridge)
    digest = bridge.approve(proposal, "person:alice")
    assert predicted.accepted is True
    assert digest == proposal.digest()
    assert predicted.state.approval_digest == predicted.state.proposal_digest


def test_apply_transition_matches_real_bridge():
    model = transition(State(), "propose").state
    model = transition(model, "approve_second").state
    predicted = transition(model, "apply")
    bridge, adapter, _, proposal = _approved()
    result = bridge.apply(proposal)
    assert predicted.accepted is True
    assert result.state == "verified"
    assert len(adapter.applied) == predicted.state.applied_count == 1


def test_reuse_transition_matches_real_bridge():
    model = transition(State(), "propose").state
    model = transition(model, "approve_second").state
    model = transition(model, "apply").state
    predicted = transition(model, "reuse")
    bridge, adapter, _, proposal = _approved()
    bridge.apply(proposal)
    with pytest.raises(ApprovalError):
        bridge.apply(proposal)
    assert predicted.accepted is False
    assert len(adapter.applied) == 1


def test_stop_transition_matches_real_bridge():
    model = transition(State(), "propose").state
    model = transition(model, "approve_second").state
    model = transition(model, "stop")
    predicted = transition(model.state, "apply")
    bridge, _, _, proposal = _approved()
    bridge.stop()
    with pytest.raises(SafetyStop):
        bridge.apply(proposal)
    assert model.accepted is True
    assert predicted.accepted is False


def test_clear_transition_matches_real_bridge():
    model = transition(State(), "propose").state
    model = transition(model, "stop").state
    predicted_clear = transition(model, "clear")
    model = predicted_clear.state
    model = transition(model, "approve_second").state
    predicted_apply = transition(model, "apply")
    bridge, _, clock = make()
    proposal = propose(bridge)
    bridge.stop()
    bridge.clear_stop()
    bridge.approve(proposal, "person:alice")
    result = bridge.apply(proposal)
    assert predicted_clear.accepted is True
    assert predicted_apply.accepted is True
    assert result.state == "verified"


def test_expire_transition_matches_real_bridge():
    model = transition(State(), "propose").state
    model = transition(model, "approve_second").state
    model = transition(model, "expire").state
    predicted = transition(model, "apply")
    bridge, adapter, clock, proposal = _approved()
    clock.t += 301.0
    with pytest.raises(ApprovalError):
        bridge.apply(proposal)
    assert predicted.accepted is False
    assert adapter.applied == []


def test_digest_change_transition_matches_real_bridge():
    model = transition(State(), "propose").state
    model = transition(model, "approve_second").state
    model = transition(model, "change_digest")
    predicted = transition(model.state, "apply")
    bridge, adapter, _, proposal = _approved()
    tampered = dataclasses.replace(proposal, value=0.9)
    with pytest.raises(ApprovalError):
        bridge.apply(tampered)
    assert model.accepted is True
    assert predicted.accepted is False
    assert adapter.applied == []


def test_bridge_rejects_approval_of_an_altered_proposal_copy():
    bridge, adapter, _ = make()
    proposal = propose(bridge)
    altered = dataclasses.replace(proposal, value=0.9)
    with pytest.raises(ApprovalError, match="differs from the validated one"):
        bridge.approve(altered, "person:alice")
    assert adapter.applied == []


def test_all_reachable_model_states_satisfy_five_invariants():
    states, failure = explore()
    assert failure is None
    assert all(not violations(state) for state in states)


@pytest.mark.parametrize(
    ("mutation", "property_name"),
    [
        ({"allow_p2_bad": True}, "P2 apply follows approval of the exact same digest"),
        ({"allow_p3_bad": True}, "P3 requester never approves own proposal"),
        ({"allow_p4_bad": True}, "P4 stop blocks apply until clear"),
        ({"allow_p5_bad": True}, "P5 expired approval never applies"),
    ],
)
def test_each_safety_property_has_a_detected_mutation(mutation, property_name):
    _, failure = explore(**mutation)
    assert failure is not None, "mutated model unexpectedly passed: " + property_name
    trace, failed_properties, bad_state = failure
    assert property_name in failed_properties
    assert trace, "the counterexample must include at least one event"


def _real_event(bridge, clock, proposal, event, *, approved=False):
    """Execute one replay event against a fresh real Bridge; return (accepted, proposal)."""
    if event == "propose":
        return True, propose(bridge)
    if event == "approve_requester":
        if proposal is None:
            return False, proposal
        try:
            bridge.approve(proposal, proposal.requested_by)
        except ApprovalError:
            return False, proposal
        return True, proposal
    if event == "approve_second":
        if proposal is None:
            return False, proposal
        try:
            bridge.approve(proposal, "person:alice")
        except ApprovalError:
            return False, proposal
        return True, proposal
    if event == "change_digest":
        if proposal is None or not approved:
            # The abstract tamper event is enabled only after approval. Keep
            # the unapproved Bridge proposal unchanged in this out-of-domain
            # event position, so a later approval still sees the validated copy.
            return False, proposal
        return True, dataclasses.replace(proposal, value=0.9)
    if event in ("apply", "reuse"):
        if proposal is None:
            return False, proposal
        try:
            bridge.apply(proposal)
        except (ApprovalError, SafetyStop):
            return False, proposal
        return True, proposal
    if event == "stop":
        bridge.stop()
        return True, proposal
    if event == "clear":
        bridge.clear_stop()
        return True, proposal
    if event == "expire":
        if proposal is None or clock.t > proposal.expires_at:
            return False, proposal
        clock.t = proposal.expires_at + 1.0
        return True, proposal
    raise AssertionError("unhandled replay event: " + event)


def test_replay_one_proposal_domain_through_length_five():
    """Replay the defined one-proposal domain; every compared result must agree."""
    from itertools import product

    disagreements = []
    sequence_count = 0
    compared_steps = 0
    compared_events = {
        "propose", "approve_requester", "approve_second",
        "apply", "reuse", "stop",
    }

    for length in range(6):
        for sequence in product(_model.EVENTS, repeat=length):
            # The real Bridge ledger is single-proposal for this replay domain.
            if sequence.count("propose") > 1:
                continue
            sequence_count += 1
            model_state = State()
            bridge, _, clock = make()
            proposal = None
            for index, event in enumerate(sequence):
                was_stopped = model_state.stopped
                predicted = transition(model_state, event)
                actual, proposal = _real_event(
                    bridge, clock, proposal, event,
                    approved=bool(model_state.approver),
                )

                compare = event in compared_events or (
                    event == "clear" and was_stopped
                )
                if compare:
                    compared_steps += 1
                    if predicted.accepted != actual:
                        disagreements.append({
                            "sequence": sequence,
                            "step": index + 1,
                            "event": event,
                            "model": predicted.accepted,
                            "bridge": actual,
                        })
                model_state = predicted.state

    print(
        "One-proposal replay:",
        f"{sequence_count} sequences;",
        f"{compared_steps} compared steps;",
        f"{len(disagreements)} disagreements",
    )
    assert sequence_count > 1000
    assert compared_steps > 100_000
    assert not disagreements, (
        "Model/Bridge accepted-flag disagreements (sequence, step, event, "
        "model answer, Bridge answer): " + repr(disagreements)
    )
