"""A simulated machine tool: the AI may only adjust a tool offset, through proposal, approval and limits."""
import pytest

from ragleap_physical import (ApprovalError, Bridge, Capability, ConfigError, Envelope, EnvelopeViolation,
                              ExpectedOutcome, SafetyStop)
from ragleap_physical.adapters import FakeMachineToolAdapter

from helpers import Clock, cfg

TOOL = {"offset_min": -60.0, "offset_max": 60.0, "step_max": 4.0, "min_gap_s": 5.0}


def tool_cfg(**tool_changes):
    t = dict(TOOL)
    t.update(tool_changes)
    return cfg(targets={"tool_1": t}, allowed_commands=["set_tool_offset"],
               episode={"max_sim_time_s": 100000, "max_steps": 1000000}, rate={"max_commands_per_s": 5})


def make(adapter=None, config=None):
    adapter = adapter or FakeMachineToolAdapter(seed=1)
    clock = Clock()
    return Bridge(adapter, Envelope(config or tool_cfg()), org_id="org_demo", max_capability=Capability.CONTROL,
                  clock=clock, max_pending=1000), adapter, clock


def propose(b, value, **kw):
    now = b.observe().sim_time_s
    return b.propose(requested_by="employee:quality", target="tool_1", command="set_tool_offset", value=value, unit="um",
                     expected_outcome=ExpectedOutcome("tool_1", "pos", value - 0.01, value + 0.01, "um", now + 1.0), **kw)


def approved(b, value):
    p = propose(b, value)
    b.approve(p, "person:alice")
    return p


def code_of(fn):
    try:
        fn()
    except EnvelopeViolation as exc:
        return exc.code
    return None


# ---- configuration
def test_offset_target_config_is_validated():
    assert Envelope(tool_cfg()).as_dict()["targets"]["tool_1"]["step_max"] == 4.0
    for bad in (dict(offset_min=5.0, offset_max=1.0), dict(step_max=0.0), dict(min_gap_s=-1.0), dict(step_max=float("nan"))):
        with pytest.raises(ConfigError):
            Envelope(tool_cfg(**bad))
    with pytest.raises(ConfigError):
        Envelope(cfg(targets={"tool_1": {"offset_min": -1.0, "offset_max": 1.0}}, allowed_commands=["set_tool_offset"]))


def test_a_joint_command_cannot_be_used_on_an_offset_target_and_the_other_way_round():
    b, adapter, _ = make()
    now = b.observe().sim_time_s
    with pytest.raises(EnvelopeViolation) as e:
        b.propose(requested_by="employee:quality", target="tool_1", command="set_position_target", value=1.0, unit="rad",
                  expected_outcome=ExpectedOutcome("tool_1", "pos", 0.9, 1.1, "rad", now + 1.0))
    assert e.value.code in ("command_not_allowed", "target_kind")


# ---- the normal path
def test_a_small_offset_change_is_applied_and_verified():
    b, adapter, _ = make()
    r = b.apply(approved(b, 3.0))
    assert r.state == "verified" and adapter.offset == 3.0 and adapter.applied == [("tool_1", "set_tool_offset", 3.0)]


# ---- the limits
def test_step_larger_than_the_maximum_is_refused():
    b, adapter, _ = make()
    assert code_of(lambda: propose(b, 4.5)) == "step" and adapter.applied == []


def test_offset_outside_the_range_is_refused():
    b, adapter, _ = make(config=tool_cfg(step_max=500.0))
    assert code_of(lambda: propose(b, 61.0)) == "range" and code_of(lambda: propose(b, -61.0)) == "range"


def test_wrong_unit_is_refused():
    b, _, _ = make()
    now = b.observe().sim_time_s
    with pytest.raises(EnvelopeViolation) as e:
        b.propose(requested_by="employee:quality", target="tool_1", command="set_tool_offset", value=1.0, unit="m",
                  expected_outcome=ExpectedOutcome("tool_1", "pos", 0.9, 1.1, "m", now + 1.0))
    assert e.value.code == "unit"


def test_changes_too_close_together_are_refused():
    b, adapter, _ = make()
    b.apply(approved(b, 2.0))
    assert code_of(lambda: propose(b, 3.0)) == "min_gap"
    adapter.advance(adapter.t + 5.0, 1000)
    b.apply(approved(b, 3.0))
    assert adapter.offset == 3.0


def test_unknown_state_fails_closed():
    class Silent(FakeMachineToolAdapter):
        def observe_raw(self):
            r = dict(super().observe_raw())
            r["sensors"] = [s for s in r["sensors"] if s["id"] != "since_change_s"]
            return r
    b, adapter, _ = make(Silent(seed=1))
    assert code_of(lambda: propose(b, 1.0)) == "state_unknown" and adapter.applied == []


def test_the_approved_value_cannot_be_changed_afterwards():
    import dataclasses
    b, adapter, _ = make()
    p = approved(b, 3.0)
    with pytest.raises(ApprovalError):
        b.apply(dataclasses.replace(p, value=40.0))
    assert adapter.applied == []


# ---- what the AI cannot do
def test_changing_the_tool_is_not_a_command():
    b, adapter, _ = make()
    now = b.observe().sim_time_s
    with pytest.raises(EnvelopeViolation) as e:
        b.propose(requested_by="employee:quality", target="tool_1", command="change_tool", value=0.0, unit="um",
                  expected_outcome=ExpectedOutcome("tool_1", "pos", -1.0, 1.0, "um", now + 1.0))
    assert e.value.code == "command_not_allowed"
    with pytest.raises(ValueError):
        adapter.apply("tool_1", "change_tool", 0.0)


def test_only_an_operator_can_replace_the_tool_and_it_resets_wear():
    b, adapter, _ = make()
    adapter.advance(100.0, 10000)
    assert adapter.wear > 20 and adapter.tool_age == 100
    adapter.replace_tool()
    assert adapter.wear == 0.0 and adapter.offset == 0.0 and adapter.tool_age == 0


def test_stop_halts_production_and_blocks_changes():
    b, adapter, _ = make()
    p = approved(b, 2.0)
    b.stop()
    with pytest.raises(SafetyStop):
        b.apply(p)
    t = adapter.t
    adapter.advance(t + 50.0, 1000)
    assert adapter.t == t


# ---- the simulation itself
def test_same_seed_same_parts():
    out = []
    for _ in range(2):
        a = FakeMachineToolAdapter(seed=7)
        a.advance(200.0, 10000)
        out.append((a.wear.hex(), a.scrap, a.last_measurement.hex()))
    assert out[0] == out[1]


def test_wear_grows_and_uncorrected_parts_become_scrap():
    a = FakeMachineToolAdapter(seed=3)
    a.advance(150.0, 10000)
    assert a.wear > 30 and a.scrap > 50


# ---- the closed loop through the bridge
def closed_loop(correct, seed, parts=150):
    b, adapter, clock = make(FakeMachineToolAdapter(seed=seed))
    refused = 0
    while adapter.t < parts:
        before = adapter.t
        adapter.advance((int(adapter.t) // 5 + 1) * 5.0, 1000)  # run to the next gauge reading
        clock.t += adapter.t - before
        if not correct:
            continue
        obs = b.observe()
        s = {x.id: x.value for x in obs.sensors}
        if s["measurement_age"] == 0 and abs(s["measured_error"]) > 3.0:
            step = max(-4.0, min(4.0, s["measured_error"]))
            try:
                b.apply(approved(b, obs.entity("tool_1").pos + step))
            except EnvelopeViolation:
                refused += 1
    return adapter.scrap, adapter.applied, refused, b


def test_closed_loop_through_the_bridge_cuts_scrap_and_every_change_was_approved():
    without = sum(closed_loop(False, s)[0] for s in range(1, 6))
    with_loop = [closed_loop(True, s) for s in range(1, 6)]
    scrap_with = sum(r[0] for r in with_loop)
    assert scrap_with < 0.4 * without, (scrap_with, without)
    for scrap, applied, refused, b in with_loop:
        assert len(applied) >= 5 and all(abs(v) <= 60.0 for _, _, v in applied)
        approvals = [e for e in b.audit if e["event"] == "approved"]
        assert len(approvals) >= len(applied) and all(e["approver"] == "person:alice" for e in approvals)


# ---- gaps found by deliberate breaks
def test_target_kinds_are_enforced_when_both_commands_are_allowed():
    from ragleap_physical import Proposal
    both = Envelope(cfg(targets={"joint_1": {"pos_min": -1.0, "pos_max": 1.0, "vel_max": 2.0, "force_max": 5.0},
                                 "tool_1": dict(TOOL)},
                        allowed_commands=["set_position_target", "set_tool_offset"]))

    def prop(target, command, unit, value):
        return Proposal("prop_000001", "org_demo", "employee:ops", target, command, value, unit,
                        ExpectedOutcome(target, "pos", value - 0.1, value + 0.1, unit, 5.0), "obs_000001", "", 1000.0, 1300.0)
    with pytest.raises(EnvelopeViolation) as e:
        both.check_static(prop("tool_1", "set_position_target", "rad", 0.5))
    assert e.value.code == "target_kind"
    with pytest.raises(EnvelopeViolation) as e:
        both.check_static(prop("joint_1", "set_tool_offset", "um", 0.5))
    assert e.value.code == "target_kind"
    both.check_static(prop("joint_1", "set_position_target", "rad", 0.5))
    both.check_static(prop("tool_1", "set_tool_offset", "um", 0.5))


def test_the_gap_is_checked_again_at_apply_time_not_only_when_proposing():
    b, adapter, _ = make()
    first, second = propose(b, 2.0), propose(b, 1.0)  # both valid now: nothing has changed yet
    b.approve(first, "person:alice")
    b.approve(second, "person:alice")
    b.apply(first)
    with pytest.raises(EnvelopeViolation) as e:
        b.apply(second)  # the world changed after approval: it is now too soon
    assert e.value.code == "min_gap" and adapter.offset == 2.0 and len(adapter.applied) == 1


def test_the_step_is_checked_again_at_apply_time():
    b, adapter, _ = make(config=tool_cfg(min_gap_s=0.0))
    first, second = propose(b, 4.0), propose(b, -3.9)  # each is within 4 um of the offset 0 right now
    b.approve(first, "person:alice")
    b.approve(second, "person:alice")
    b.apply(first)
    with pytest.raises(EnvelopeViolation) as e:
        b.apply(second)  # from 4.0 to -3.9 is a step of 7.9 um
    assert e.value.code == "step" and adapter.offset == 4.0


def test_change_tool_cannot_be_put_in_the_envelope_either():
    with pytest.raises(ConfigError):
        Envelope(cfg(targets={"tool_1": dict(TOOL)}, allowed_commands=["change_tool"]))
