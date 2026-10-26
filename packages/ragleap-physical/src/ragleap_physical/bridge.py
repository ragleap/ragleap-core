"""The bridge: capability checks, proposals, approvals, the safety envelope, apply and verify.

Path for a command: proposal -> envelope check -> approval bound to the exact proposal ->
fresh re-check -> dry run -> apply in short slices -> outcome check. Any unexpected error goes to the
safe state. Only simulated adapters are accepted in this version.

Threads: all calls take one lock, except ``stop()``. ``stop()`` sets a flag at once and never waits for a
running apply; the apply notices the flag between slices (``chunk_s`` of simulated time) and stops.
Memory: pending proposals, kept records and the in-memory audit list are all capped.
"""
from __future__ import annotations

import functools
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .adapters.base import Adapter
from .capability import Capability
from .envelope import Envelope
from .errors import ApprovalError, CapabilityError, ConfigError, EnvelopeViolation, SafetyStop, ValidationError
from .observation import Observation, actor, ident
from .proposal import ExpectedOutcome, Proposal

_LIVE = ("validated", "approved", "applying", "dry_run_ok", "applied")  # records that must not be evicted


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of one apply. ``outcome_ok`` comes from a real check of the state, not from a model."""

    proposal_id: str
    state: str
    outcome_ok: bool
    final_observation: Observation
    proposal_digest: str
    envelope_digest: str


class _StopRequested(Exception):
    """Internal: stop was requested while an apply was running."""


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            try:
                return fn(self, *args, **kwargs)
            finally:
                self._enforce_stop()
    return wrapper


class Bridge:
    """Holds one adapter, one envelope and the approval ledger for one organization."""

    def __init__(self, adapter: Adapter, envelope: Envelope, *, org_id: str, max_capability: Capability,
                 clock: Callable[[], float] = time.time, ttl_s: float = 300.0,
                 audit_sink: Optional[Callable[[dict], None]] = None, max_pending: int = 50,
                 max_records: int = 1000, max_audit: int = 10000, chunk_s: float = 0.25) -> None:
        ident(org_id, "org_id")
        if not adapter.simulated:
            raise CapabilityError("real hardware is not supported in this version")
        if not isinstance(max_capability, Capability):
            raise CapabilityError("max_capability must be a Capability")
        if min(max_pending, max_records, max_audit) < 1 or not chunk_s > 0:
            raise ConfigError("max_pending, max_records, max_audit and chunk_s must be positive")
        self._adapter, self._env, self.org_id = adapter, envelope, org_id
        self._max, self._clock, self._ttl, self._sink = max_capability, clock, float(ttl_s), audit_sink
        self._max_pending, self._max_records, self._max_audit, self._chunk_s = max_pending, max_records, max_audit, chunk_s
        self._ledger: dict = {}
        self._applied_at: deque = deque()
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._adapter_held = False
        self._n_obs = self._n_prop = 0
        self.audit: list = []

    @property
    def max_capability(self) -> Capability:
        """The highest capability level this bridge offers."""
        return self._max

    # ---- helpers
    def _need(self, level: Capability) -> None:
        if self._max < level:
            raise CapabilityError(f"this bridge offers at most {self._max.name.lower()}")

    def _log(self, event: str, **fields: Any) -> None:
        rec = {"t": self._clock(), "event": event, "envelope": self._env.digest, **fields}
        with self._log_lock:
            self.audit.append(rec)
            extra = len(self.audit) - self._max_audit
            if extra > 0:
                del self.audit[:extra]  # memory cap; the sink (if any) has every entry
        if self._sink:
            self._sink(rec)

    def _safe_stop(self, why: str) -> None:
        self._stop_flag.set()
        try:
            self._adapter.stop()
            self._adapter_held = True
        finally:
            self._log("safe_stop", why=why)

    def _enforce_stop(self) -> None:
        """Called with the lock held: if a stop was requested and the adapter is not yet held, hold it."""
        if self._stop_flag.is_set() and not self._adapter_held:
            self._safe_stop("stop requested")

    # ---- observe / view
    @_locked
    def observe(self) -> Observation:
        """Read state. org_id is set here from our own config, never from the device."""
        self._need(Capability.OBSERVE)
        self._n_obs += 1
        return Observation.from_raw(self._adapter.observe_raw(), org_id=self.org_id,
                                    observation_id=f"obs_{self._n_obs:06d}")

    @_locked
    def envelope_view(self) -> dict:
        """Read-only copy of the limits."""
        self._need(Capability.OBSERVE)
        return self._env.as_dict()

    # ---- propose / approve
    def _expire_and_count(self, now: float) -> int:
        pending = 0
        for rec in self._ledger.values():
            if rec["state"] in ("validated", "approved"):
                if now > rec["proposal"].expires_at:
                    rec["state"] = "expired"
                else:
                    pending += 1
        return pending

    def _evict(self) -> None:
        over = len(self._ledger) - self._max_records
        if over <= 0:
            return
        for pid in [p for p, r in self._ledger.items() if r["state"] not in _LIVE][:over]:
            del self._ledger[pid]  # replay stays refused: an unknown proposal has no approval

    @_locked
    def propose(self, *, requested_by: str, target: str, command: str, value: float, unit: str,
                expected_outcome: ExpectedOutcome, reason: str = "") -> Proposal:
        """Return a validated proposal. Nothing is sent to the device."""
        self._need(Capability.RECOMMEND)
        now = self._clock()
        if self._expire_and_count(now) >= self._max_pending:
            self._log("rejected", code="pending_limit")
            raise EnvelopeViolation("pending_limit", "too many proposals are waiting for approval")
        obs = self.observe()
        self._n_prop += 1
        p = Proposal(f"prop_{self._n_prop:06d}", self.org_id, requested_by, target, command, value, unit,
                     expected_outcome, obs.observation_id, reason, now, now + self._ttl)
        self._env.check_state(obs)
        self._env.check_static(p)
        self._ledger[p.proposal_id] = {"proposal": p, "digest": p.digest(), "state": "validated", "approval": None}
        self._evict()
        self._log("validated", proposal_id=p.proposal_id, digest=p.digest())
        return p

    @_locked
    def get_proposal(self, proposal_id: str) -> Proposal:
        """Operator side: fetch a proposal to show to the person who approves. Not a model tool."""
        rec = self._ledger.get(proposal_id)
        if rec is None:
            raise ApprovalError("unknown proposal")
        return rec["proposal"]

    @_locked
    def approve(self, proposal: Proposal, approver: str) -> str:
        """Record an approval bound to the exact proposal shown. Returns its digest."""
        rec = self._ledger.get(proposal.proposal_id)
        if rec is None or rec["state"] != "validated":
            raise ApprovalError("proposal is unknown or not waiting for approval")
        try:
            actor(approver, "approver")
        except ValidationError:
            raise ApprovalError("approver must be a named person id") from None
        if proposal.digest() != rec["digest"]:
            raise ApprovalError("proposal differs from the validated one")
        if self._clock() > proposal.expires_at:
            rec["state"] = "expired"
            raise ApprovalError("proposal has expired")
        rec["approval"] = {"approver": approver, "digest": rec["digest"], "t": self._clock()}
        rec["state"] = "approved"
        self._log("approved", proposal_id=proposal.proposal_id, approver=approver, digest=rec["digest"])
        return rec["digest"]

    # ---- apply
    @_locked
    def apply(self, proposal: Proposal) -> ApplyResult:
        """Apply an approved proposal in simulation, then verify the expected outcome."""
        self._need(Capability.CONTROL)
        if self._stop_flag.is_set():
            self._log("rejected", proposal_id=proposal.proposal_id, code="stopped")
            raise SafetyStop("the bridge is stopped")
        rec = self._ledger.get(proposal.proposal_id)
        if rec is None or rec["state"] != "approved":
            state = "unknown" if rec is None else rec["state"]
            self._log("rejected", proposal_id=proposal.proposal_id, code="no_valid_approval")
            raise ApprovalError(f"no valid approval (state: {state})")
        digest = proposal.digest()
        if digest != rec["approval"]["digest"] or digest != rec["digest"]:
            self._log("rejected", proposal_id=proposal.proposal_id, code="changed_after_approval")
            raise ApprovalError("proposal changed after approval")
        if self._clock() > proposal.expires_at:
            rec["state"] = "expired"
            self._log("rejected", proposal_id=proposal.proposal_id, code="expired")
            raise ApprovalError("approval has expired")
        rec["state"] = "applying"  # single use: consumed even if a later check fails
        actuated = False
        try:
            obs = self.observe()
            self._env.check_state(obs)
            self._env.check_static(proposal)
            self._check_rate()
            o = proposal.expected_outcome
            if o.by_sim_time_s <= obs.sim_time_s:
                raise EnvelopeViolation("outcome_time", "expected outcome time is already past")
            pred = self._adapter.dry_run(proposal.target, proposal.command, proposal.value, o.by_sim_time_s,
                                         self._env.max_steps)
            self._env.check_prediction(proposal.target, pred.max_abs_vel, pred.max_abs_force)
            rec["state"] = "dry_run_ok"
            self._applied_at.append(self._clock())
            self._adapter.apply(proposal.target, proposal.command, proposal.value)
            actuated = True
            rec["state"] = "applied"
            self._run_to(o.by_sim_time_s, obs.step)
            final = self.observe()
            ok = self._outcome_ok(final, o)
            rec["state"] = "verified" if ok else "failed"
            self._log(rec["state"], proposal_id=proposal.proposal_id, digest=digest)
            return ApplyResult(proposal.proposal_id, rec["state"], ok, final, digest, self._env.digest)
        except _StopRequested:
            rec["state"] = "stopped"
            self._log("rejected", proposal_id=proposal.proposal_id, code="stopped_during_apply")
            self._safe_stop("stop requested during apply")
            raise SafetyStop("stopped while the command was running") from None
        except EnvelopeViolation as exc:
            rec["state"] = "rejected"
            self._log("rejected", proposal_id=proposal.proposal_id, code=exc.code)
            if actuated:
                self._safe_stop(exc.code)
            raise
        except Exception as exc:
            rec["state"] = "failed"
            self._safe_stop(type(exc).__name__)
            raise

    def _run_to(self, target_t: float, start_step: int) -> None:
        """Advance in slices so a stop request is honoured within ``chunk_s`` of simulated time."""
        budget = self._env.max_steps
        while True:
            if self._stop_flag.is_set():
                raise _StopRequested()
            now = self.observe()
            if now.sim_time_s >= target_t - 1e-12:
                return
            remaining = budget - (now.step - start_step)
            if remaining <= 0:
                raise EnvelopeViolation("max_steps", "step limit reached before the expected time")
            self._adapter.advance(min(now.sim_time_s + self._chunk_s, target_t), remaining)
            if self.observe().step == now.step:
                raise EnvelopeViolation("no_progress", "the simulation did not advance")

    def _check_rate(self) -> None:
        now = self._clock()
        while self._applied_at and now - self._applied_at[0] >= 1.0:
            self._applied_at.popleft()
        if len(self._applied_at) >= self._env.max_commands_per_s:
            raise EnvelopeViolation("rate", "too many commands per second")

    @staticmethod
    def _outcome_ok(obs: Observation, o: ExpectedOutcome) -> bool:
        try:
            e = obs.entity(o.entity)
        except ValidationError:
            return False
        value, unit = (e.pos, e.pos_unit) if o.quantity == "pos" else (e.vel, e.vel_unit)
        return unit == o.unit and o.lo <= value <= o.hi

    # ---- stop
    def stop(self) -> None:
        """Safe state. Needs no approval, no capability and never waits for a running apply.

        The flag is set at once. If nothing is running we hold the adapter now; otherwise the running call
        holds it (an apply notices within ``chunk_s``, any other call does it when it finishes).
        """
        self._stop_flag.set()
        if self._lock.acquire(blocking=False):
            try:
                self._enforce_stop()
            finally:
                self._lock.release()
        else:
            self._log("stop_requested_while_busy")

    @_locked
    def clear_stop(self) -> None:
        """Operator only. Do not expose this through any model tool."""
        self._adapter.clear_stop()
        self._stop_flag.clear()
        self._adapter_held = False
        self._log("stop_cleared")
