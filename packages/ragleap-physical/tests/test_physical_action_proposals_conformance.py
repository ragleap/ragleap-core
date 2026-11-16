from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Protocol
import unicodedata

import pytest

from helpers import make, propose
from ragleap_physical.errors import ApprovalError, SafetyStop
from ragleap_physical.proposal import Proposal


class PhysicalActionTarget(Protocol):
    """Test-only contract shared by independent targets; not a production API."""

    def propose(self, **kwargs) -> Proposal: ...

    def approve(self, proposal: Proposal, approver: str) -> str: ...

    def apply(self, proposal: Proposal) -> Any: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class FakeResult:
    proposal_id: str
    state: str
    outcome_ok: bool


class IndependentFakeTarget:
    """Small in-memory implementation, independent of Bridge and Adapter."""

    def __init__(self):
        self._proposals = {}
        self._approvals = {}
        self._stopped = False

    def propose(self, *, requested_by, target, command, value, unit, expected_outcome, reason=""):
        proposal = Proposal(
            proposal_id="fake_000001",
            org_id="org_demo",
            requested_by=requested_by,
            target=target,
            command=command,
            value=value,
            value_unit=unit,
            expected_outcome=expected_outcome,
            state_ref="fake_observation",
            reason=reason,
            created_at=1000.0,
            expires_at=1300.0,
        )
        self._proposals[proposal.proposal_id] = proposal.digest()
        return proposal

    def approve(self, proposal, approver):
        expected = self._proposals.get(proposal.proposal_id)
        requester = unicodedata.normalize("NFKC", proposal.requested_by).casefold().strip()
        approver_id = unicodedata.normalize("NFKC", approver).casefold().strip()
        if (expected is None or proposal.digest() != expected
                or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,64}", approver)
                or approver_id == requester):
            raise ApprovalError("proposal cannot be approved")
        self._approvals[proposal.proposal_id] = expected
        return expected

    def apply(self, proposal):
        if self._stopped:
            raise SafetyStop("target is stopped")
        expected = self._proposals.get(proposal.proposal_id)
        approval = self._approvals.get(proposal.proposal_id)
        if expected is None or approval != expected or proposal.digest() != expected:
            raise ApprovalError("proposal has no matching approval")
        del self._approvals[proposal.proposal_id]
        return FakeResult(proposal.proposal_id, "verified", True)

    def stop(self):
        self._stopped = True


def _bridge_target():
    return make()[0]


IMPLEMENTATIONS = (
    pytest.param(_bridge_target, id="ragleap-bridge"),
    pytest.param(IndependentFakeTarget, id="independent-fake"),
)


@pytest.fixture(params=IMPLEMENTATIONS)
def target(request) -> PhysicalActionTarget:
    return request.param()


def test_target_proposes_and_applies_with_approval(target):
    proposal = propose(target)

    assert isinstance(proposal, Proposal)
    assert proposal.proposal_id
    assert target.approve(proposal, "person:alice") == proposal.digest()

    result = target.apply(proposal)

    assert result.proposal_id == proposal.proposal_id
    assert result.outcome_ok is True
    assert result.state in {"verified", "applied"}


def test_target_refuses_self_approval(target):
    proposal = propose(target)

    with pytest.raises(ApprovalError):
        target.approve(proposal, "EMPLOYEE:OPS")


def test_target_rejects_tampering_after_approval(target):
    proposal = propose(target)
    target.approve(proposal, "person:alice")
    tampered = replace(proposal, reason="changed after approval")

    with pytest.raises(ApprovalError):
        target.apply(tampered)


def test_target_refuses_apply_after_stop(target):
    proposal = propose(target)
    target.approve(proposal, "person:alice")
    target.stop()

    with pytest.raises(SafetyStop):
        target.apply(proposal)
