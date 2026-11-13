"""A stop invalidates every approval given before it (issue 1338, option A)."""
import pytest

from ragleap_physical import ApprovalError

from helpers import make, propose

ALICE = "person:alice"


def test_an_approval_given_before_a_stop_is_refused_after_the_clear():
    bridge, adapter, _clock = make()
    proposal = propose(bridge)
    bridge.approve(proposal, ALICE)
    bridge.stop()
    bridge.clear_stop()
    with pytest.raises(ApprovalError):
        bridge.apply(proposal)
    assert adapter.applied == []


def test_a_fresh_approval_after_the_clear_applies():
    bridge, adapter, _clock = make()
    proposal = propose(bridge)
    bridge.approve(proposal, ALICE)
    bridge.stop()
    bridge.clear_stop()
    bridge.approve(proposal, ALICE)
    assert bridge.apply(proposal).state == "verified"
    assert len(adapter.applied) == 1


def test_clear_without_a_stop_does_not_revoke():
    bridge, _adapter, _clock = make()
    proposal = propose(bridge)
    bridge.approve(proposal, ALICE)
    bridge.clear_stop()
    assert bridge.apply(proposal).state == "verified"


def test_every_outstanding_approval_is_revoked_and_each_is_logged():
    bridge, adapter, _clock = make()
    first, second = propose(bridge, value=0.5), propose(bridge, value=0.6)
    bridge.approve(first, ALICE)
    bridge.approve(second, ALICE)
    bridge.stop()
    bridge.clear_stop()
    assert len([r for r in bridge.audit if r["event"] == "approval_revoked_by_stop"]) == 2
    for proposal in (first, second):
        with pytest.raises(ApprovalError):
            bridge.apply(proposal)
    assert adapter.applied == []


def test_an_approval_given_while_stopped_is_also_revoked_at_the_clear():
    bridge, adapter, _clock = make()
    proposal = propose(bridge)
    bridge.stop()
    bridge.approve(proposal, ALICE)
    bridge.clear_stop()
    with pytest.raises(ApprovalError):
        bridge.apply(proposal)
    assert adapter.applied == []
