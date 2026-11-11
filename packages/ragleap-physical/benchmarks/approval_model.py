"""Finite explicit-state model of the Bridge approval flow.

P3 is enforced here by a transition (approve_requester) that is always rejected;
its real enforcement is checked by the corresponding Bridge test.

This is a hand-written abstract model, not a proof about the Bridge code.
The conformance replay is intentionally limited to one proposal and real
Bridge operations. Multiple-proposal sequences, idempotent clear calls made
while the model is not stopped, and the test-only change_digest and expire
events are outside that comparison domain and can produce mapping differences.
The original P1-P5 model abstracts time as a discrete expiry event. The
issue #1337-#1339 extension adds identity normalization, stop invalidation as
a pending design property, and explicit stop-race slots. The adapter-call
boundary race is modelled explicitly; general concurrency and simulator
dynamics remain outside the model.

Run with: python benchmarks/approval_model.py
Standard library only; no TLA+ or simulator is required.
"""
from collections import deque
import dataclasses
import unicodedata
from dataclasses import dataclass, replace
from typing import Optional, Tuple


@dataclass(frozen=True)
class State:
    proposed: bool = False
    requester: str = ""
    proposal_digest: str = ""
    approver: str = ""
    approval_digest: str = ""
    applied_count: int = 0
    applied_approval_digest: str = ""
    applied_proposal_digest: str = ""
    stopped: bool = False
    stop_pending: bool = False
    applied_since_stop: bool = False
    expired: bool = False
    applied_after_expiry: bool = False


@dataclass(frozen=True)
class Edge:
    event: str
    accepted: bool
    state: State


def transition(
    state: State,
    event: str,
    *,
    allow_reuse: bool = False,
    allow_p2_bad: bool = False,
    allow_p3_bad: bool = False,
    allow_p4_bad: bool = False,
    allow_p5_bad: bool = False,
) -> Edge:
    """Apply one named abstract event; rejected attempts leave state unchanged."""
    s = state
    accepted = False
    if event == "propose":
        if not s.proposed:
            s = replace(s, proposed=True, requester="employee:ops",
                        proposal_digest="digest-A")
            accepted = True
    elif event == "approve_requester":
        # P3's normal transition is always rejected; the mutation enables it.
        if allow_p3_bad and s.proposed and not s.approver and not s.expired:
            s = replace(s, approver=s.requester,
                        approval_digest=s.proposal_digest)
            accepted = True
    elif event == "approve_second":
        if s.proposed and not s.expired and not s.approver:
            s = replace(s, approver="person:alice",
                        approval_digest=s.proposal_digest)
            accepted = True
    elif event == "change_digest":
        # Tampering is meaningful only after the exact proposal was approved.
        # Before approval, the Bridge rejects approval of the altered copy.
        if s.proposed and s.approver and not s.applied_count:
            s = replace(s, proposal_digest="digest-B")
            accepted = True
    elif event == "apply":
        approval_matches = bool(s.approver) and (
            s.approval_digest == s.proposal_digest
        )
        reusable = s.applied_count == 0
        if (s.proposed and (approval_matches or allow_p2_bad)
                and (not s.stopped or allow_p4_bad)
                and (not s.expired or allow_p5_bad) and reusable):
            s = replace(
                s,
                applied_count=s.applied_count + 1,
                applied_approval_digest=s.approval_digest,
                applied_proposal_digest=s.proposal_digest,
                applied_since_stop=s.applied_since_stop or s.stop_pending,
                applied_after_expiry=s.applied_after_expiry or s.expired,
            )
            accepted = True
    elif event == "stop":
        s = replace(s, stopped=True, stop_pending=True,
                    applied_since_stop=False)
        accepted = True
    elif event == "clear":
        if s.stopped:
            s = replace(s, stopped=False, stop_pending=False,
                        applied_since_stop=False)
            accepted = True
    elif event == "expire":
        if s.proposed and not s.expired:
            s = replace(s, expired=True)
            accepted = True
    elif event == "reuse":
        # A replay is the same apply request after the first successful apply.
        if (allow_reuse and s.applied_count == 1 and s.proposed
                and s.approver and s.approval_digest == s.proposal_digest
                and not s.stopped and not s.expired):
            s = replace(s, applied_count=2,
                        applied_approval_digest=s.approval_digest,
                        applied_proposal_digest=s.proposal_digest,
                        applied_since_stop=s.applied_since_stop or s.stop_pending)
            accepted = True
        else:
            edge = transition(s, "apply")
            return Edge(event, edge.accepted, edge.state)
    else:
        raise ValueError("unknown event: " + event)
    return Edge(event, accepted, s)


EVENTS = (
    "propose", "approve_requester", "approve_second", "change_digest",
    "apply", "stop", "clear", "expire", "reuse",
)


def violations(s: State) -> Tuple[str, ...]:
    failures = []
    if s.applied_count > 1:
        failures.append("P1 proposal applied at most once")
    if s.applied_count and not (
        s.applied_approval_digest
        == s.applied_proposal_digest
        == s.approval_digest
    ):
        failures.append("P2 apply follows approval of the exact same digest")
    if s.approver and s.approver == s.requester:
        failures.append("P3 requester never approves own proposal")
    if s.stop_pending and s.applied_since_stop:
        failures.append("P4 stop blocks apply until clear")
    if s.applied_after_expiry:
        failures.append("P5 expired approval never applies")
    return tuple(failures)


def explore(
    *,
    allow_reuse: bool = False,
    allow_p2_bad: bool = False,
    allow_p3_bad: bool = False,
    allow_p4_bad: bool = False,
    allow_p5_bad: bool = False,
):
    """Breadth-first exhaustive exploration; return states and first counterexample."""
    initial = State()
    queue = deque([(initial, ())])
    seen = {initial}
    first_failure = None
    while queue:
        state, trace = queue.popleft()
        failed = violations(state)
        if failed and first_failure is None:
            first_failure = (trace, failed, state)
        for event in EVENTS:
            edge = transition(
                state, event, allow_reuse=allow_reuse,
                allow_p2_bad=allow_p2_bad, allow_p3_bad=allow_p3_bad,
                allow_p4_bad=allow_p4_bad, allow_p5_bad=allow_p5_bad,
            )
            if edge.state not in seen:
                seen.add(edge.state)
                queue.append((edge.state, trace + ((event, edge.accepted),)))
    return seen, first_failure


def main():
    states, failure = explore()
    print("Approval-flow explicit-state model")
    print("Reachable states:", len(states))
    properties = (
        "P1 proposal applied at most once",
        "P2 apply follows approval of the exact same digest",
        "P3 requester never approves own proposal",
        "P4 stop blocks apply until clear",
        "P5 expired approval never applies",
    )
    if failure is None:
        for prop in properties:
            print("PASS:", prop)
        print("Counter-example trace: none (all reachable states satisfy all properties)")
    else:
        trace, failed_properties, bad_state = failure
        print("FAIL:", "; ".join(failed_properties))
        print("Counter-example trace:")
        for index, (event, accepted) in enumerate(trace, 1):
            print("  %d. %s (%s)" % (index, event, "accepted" if accepted else "rejected"))
        print("  violating state:", bad_state)
    print("Exploration: breadth-first; events:", ", ".join(EVENTS))
    print("Coverage limits: time is a discrete expire event; concurrency/races are not modeled;")
    print("the adapter, simulation dynamics, and outcome verification are abstracted away.")



# ---- Issue #1337-#1339: identity and stop semantics extension

def identity_key(value: str) -> str:
    """Canonical comparison key: compatibility normalization + caseless match."""
    if not isinstance(value, str):
        raise TypeError("identity must be text")
    return unicodedata.normalize("NFKC", value).casefold()


@dataclass(frozen=True)
class Approval:
    proposal_id: str
    requester: str
    approver: str
    digest: str
    valid: bool = True


@dataclass(frozen=True)
class StopModel:
    stopped: bool = False
    approvals: tuple[Approval, ...] = ()
    command_after_stop: bool = False


def model_event(state: StopModel, event: str, approval: Approval | None = None) -> StopModel:
    """Apply P6/P7 abstract event; events intentionally expose the P7 race slot."""
    if event == "stop":
        return dataclasses.replace(state, stopped=True,
                                   approvals=tuple(dataclasses.replace(a, valid=False) for a in state.approvals))
    if event == "clear":
        return dataclasses.replace(state, stopped=False)
    if event == "approve":
        if approval is None:
            raise ValueError("approval required")
        if identity_key(approval.requester) == identity_key(approval.approver):
            return state
        return dataclasses.replace(state, approvals=state.approvals + (approval,))
    if event == "dry_run_complete":
        return state
    if event == "stop_after_recheck_before_adapter":
        return dataclasses.replace(state, stopped=True)
    if event == "adapter_command":
        return dataclasses.replace(state, command_after_stop=state.command_after_stop or state.stopped)
    raise ValueError("unknown event: " + event)


def p6_violation(state: StopModel) -> bool:
    """P6 violation: an approval remains valid after stop."""
    return state.stopped and any(a.valid for a in state.approvals)


def p7_violation(state: StopModel) -> bool:
    """P7 violation: an adapter command occurs while stopped."""
    return state.command_after_stop


def p8_self_approval(requester: str, approver: str) -> bool:
    """True if identities denote the same person under P8 semantics."""
    return identity_key(requester) == identity_key(approver)


# Small, explicit multi-proposal replay domain. Each row is a sequence of
# (operation, proposal-slot, actor-override) tuples. Keeping named sequences
# makes any disagreement directly reproducible and reviewable.
MULTI_PROPOSAL_SEQUENCES = (
    ("P8-case-self-approval", ("propose", "A", ""), ("approve", "A", "EMPLOYEE:OPS")),
    ("P8-form-self-approval", ("propose", "A", "employee:ops"), ("approve", "A", "ｅｍｐｌｏｙｅｅ:ｏｐｓ")),
    ("P6-stop-invalidates-A", ("propose", "A", ""), ("approve", "A", "person:alice"),
     ("stop", "", ""), ("clear", "", ""), ("apply", "A", "")),
    ("P6-stop-invalidates-multiple", ("propose", "A", ""), ("propose", "B", ""),
     ("approve", "A", "person:alice"), ("approve", "B", "person:bob"),
     ("stop", "", ""), ("clear", "", ""), ("apply", "A", "")),
)


def replay_multi_proposal(bridge_factory, propose_fn, *, sequences=MULTI_PROPOSAL_SEQUENCES):
    """Run named multi-proposal sequences; return exact model/Bridge disagreements.

    `bridge_factory()` returns (bridge, adapter, clock). `propose_fn(bridge)`
    creates a real proposal. Each scenario gets a fresh Bridge.
    """
    from ragleap_physical import ApprovalError, SafetyStop
    findings = []
    for named in sequences:
        name, *events = named
        bridge, adapter, _clock = bridge_factory()
        proposals = {}
        approvals = {}
        invalidated = set()
        stopped = False
        for index, (event, slot, actor) in enumerate(events, 1):
            model_answer = "accept"
            bridge_answer = "accept"
            try:
                if event == "propose":
                    proposals[slot] = propose_fn(bridge)
                elif event == "approve":
                    p = proposals[slot]
                    requester = p.requested_by
                    model_answer = "reject" if p8_self_approval(requester, actor) else "accept"
                    bridge.approve(p, actor)
                    approvals[slot] = actor
                elif event == "stop":
                    bridge.stop(); stopped = True; invalidated.update(approvals)
                elif event == "clear":
                    bridge.clear_stop(); stopped = False
                elif event == "apply":
                    p = proposals[slot]
                    model_answer = "reject" if stopped or slot in invalidated else "accept"
                    if stopped:
                        raise SafetyStop("model expects stopped")
                    bridge.apply(p)
            except (ApprovalError, SafetyStop):
                bridge_answer = "reject"
            if model_answer != bridge_answer:
                findings.append({"sequence": name, "step": index, "event": event,
                                 "proposal": slot or None, "model": model_answer,
                                 "bridge": bridge_answer})
    return findings


if __name__ == "__main__":
    main()
