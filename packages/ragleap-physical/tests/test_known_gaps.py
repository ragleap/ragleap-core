"""Known gaps in the Bridge, written as tests first (pre-alpha, simulation only).

Each test states the behaviour we want. Each is marked ``xfail(strict=True)``: while the gap exists the test
fails as expected and CI stays green; once the gap is fixed the test passes, strict mode reports it, and the
marker must be removed in the same change. Do not weaken a test to make it pass.
"""
import pytest

from ragleap_physical.errors import ApprovalError, SafetyStop

from helpers import make, propose

PERSON = "person:alice"


def test_self_approval_with_other_letter_case_is_refused():
    """Gap 1: the requester ("employee:ops") must not approve its own proposal in another letter case."""
    for approver in ("EMPLOYEE:OPS", "Employee:Ops"):
        bridge, _adapter, _clock = make()
        proposal = propose(bridge)
        with pytest.raises(ApprovalError):
            bridge.approve(proposal, approver)
        assert [p.proposal_id for p in bridge.pending_proposals()] == [proposal.proposal_id]


def test_stop_invalidates_an_approval_given_before_it():
    """An approval given before a stop must not survive stop and clear (issue 1338: a stop invalidates earlier approvals)."""
    bridge, _adapter, _clock = make()
    proposal = propose(bridge)
    bridge.approve(proposal, PERSON)
    before = bridge.observe().entity("joint_1").pos
    bridge.stop()
    bridge.clear_stop()
    with pytest.raises(ApprovalError):
        bridge.apply(proposal)
    assert bridge.observe().entity("joint_1").pos == before


def test_no_adapter_command_when_stop_lands_during_the_dry_run():
    """Gap 3: if stop() arrives during the dry run, the adapter must not receive the command at all."""
    bridge, adapter, _clock = make()
    proposal = propose(bridge)
    bridge.approve(proposal, PERSON)
    sent = []
    real_dry_run, real_apply = adapter.dry_run, adapter.apply

    def dry_run_then_stop(*args, **kwargs):
        result = real_dry_run(*args, **kwargs)
        bridge.stop()
        return result

    def recording_apply(*args, **kwargs):
        sent.append(args)
        return real_apply(*args, **kwargs)

    adapter.dry_run, adapter.apply = dry_run_then_stop, recording_apply
    with pytest.raises(SafetyStop):
        bridge.apply(proposal)
    assert sent == []
