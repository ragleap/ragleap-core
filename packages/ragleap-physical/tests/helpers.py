"""Shared test helpers. No network, no hardware."""
import copy

from ragleap_physical import Bridge, Capability, Envelope, ExpectedOutcome
from ragleap_physical.adapters import FakeJointAdapter

BASE = {
    "envelope_version": 0, "allowed_commands": ["set_position_target"],
    "targets": {"joint_1": {"pos_min": -1.0, "pos_max": 1.0, "vel_max": 2.0, "force_max": 5.0}},
    "rate": {"max_commands_per_s": 5}, "episode": {"max_sim_time_s": 60, "max_steps": 100000},
    "on_violation": "reject",
}


class Clock:
    """A fake clock so expiry and rate tests need no sleeping."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def cfg(**changes):
    c = copy.deepcopy(BASE)
    for k, v in changes.items():
        c[k] = v
    return c


def make(level=Capability.CONTROL, config=None, adapter=None, clock=None, ttl_s=300.0):
    clock = clock or Clock()
    adapter = adapter or FakeJointAdapter()
    return Bridge(adapter, Envelope(config or BASE), org_id="org_demo", max_capability=level,
                  clock=clock, ttl_s=ttl_s), adapter, clock


def outcome(lo=0.45, hi=0.55, by=5.0):
    return ExpectedOutcome("joint_1", "pos", lo, hi, "rad", by)


def propose(bridge, value=0.5, **kw):
    kw.setdefault("expected_outcome", outcome())
    return bridge.propose(requested_by="employee:ops", target="joint_1", command="set_position_target",
                          value=value, unit="rad", **kw)
