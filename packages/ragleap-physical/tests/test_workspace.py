"""Workspace limits (a box in x, y, z) on a two-link arm, standard library only."""
import math

import pytest

from ragleap_physical import (ApprovalError, Bridge, Capability, ConfigError, Envelope, EnvelopeViolation,
                              ExpectedOutcome)
from ragleap_physical.adapters import DryRun, FakeArmAdapter, FakeJointAdapter

from helpers import Clock, cfg

JOINT = {"pos_min": -1.2, "pos_max": 1.2, "vel_max": 10.0, "force_max": 10.0}
BOX = {"x": [0.3, 1.0], "y": [-0.1, 0.1], "z": [0.4, 1.5]}


def arm_cfg(**changes):
    base = dict(targets={"joint_1": dict(JOINT), "joint_2": dict(JOINT)}, workspace=BOX)
    base.update(changes)
    return cfg(**base)


def make(adapter=None, config=None):
    adapter = adapter or FakeArmAdapter()
    return Bridge(adapter, Envelope(config or arm_cfg()), org_id="org_demo", max_capability=Capability.CONTROL,
                  clock=Clock()), adapter


def propose(b, joint="joint_1", value=0.5, lo=0.45, hi=0.55, by=6.0):
    return b.propose(requested_by="employee:ops", target=joint, command="set_position_target", value=value,
                     unit="rad", expected_outcome=ExpectedOutcome(joint, "pos", lo, hi, "rad", by))


def approved(b, **kw):
    p = propose(b, **kw)
    b.approve(p, "person:alice")
    return p


def code_of(fn):
    try:
        fn()
    except EnvelopeViolation as exc:
        return exc.code
    return None


def z(adapter):
    return adapter._site(adapter.pos)[2]


# ---- configuration
def test_workspace_config_is_validated():
    assert Envelope(arm_cfg()).as_dict()["workspace"]["z"] == [0.4, 1.5]
    for bad in ({"x": [0, 1], "y": [0, 1]}, {"x": [1, 0], "y": [0, 1], "z": [0, 1]}, {"x": [0, 1], "y": [0, 1], "z": [0]},
                {"x": [0, math.nan], "y": [0, 1], "z": [0, 1]}, {"x": [0, 1], "y": [0, 1], "z": [0, 1], "w": [0, 1]}, "box"):
        with pytest.raises(ConfigError):
            Envelope(arm_cfg(workspace=bad))


def test_workspace_changes_the_digest():
    assert Envelope(arm_cfg()).digest != Envelope(arm_cfg(workspace={"x": [0.3, 1.0], "y": [-0.1, 0.1], "z": [0.5, 1.5]})).digest


def test_envelope_must_list_every_joint_the_adapter_can_move():
    only_one = arm_cfg(targets={"joint_1": dict(JOINT)})
    with pytest.raises(ConfigError):
        Bridge(FakeArmAdapter(), Envelope(only_one), org_id="org_demo", max_capability=Capability.CONTROL)


# ---- the normal path
def test_a_move_inside_the_box_is_verified():
    b, adapter = make()
    r = b.apply(approved(b))
    assert r.state == "verified" and 0.3 <= adapter._site(adapter.pos)[0] <= 1.0 and z(adapter) >= 0.4
    sensors = {s.id: s.value for s in b.observe().sensors}
    assert set(sensors) == {"ee_x", "ee_y", "ee_z"}


def test_second_joint_can_be_moved_too():
    b, adapter = make()
    b.apply(approved(b))
    r = b.apply(approved(b, joint="joint_2", value=0.5, by=12.0))
    assert r.state == "verified" and abs(adapter.pos[1] - 0.5) < 0.05


# ---- trying to break it
def test_end_pose_outside_the_box_is_refused_before_any_motion():
    b, adapter = make()
    p = approved(b, value=1.0, lo=0.95, hi=1.05)
    assert code_of(lambda: b.apply(p)) == "workspace"
    assert adapter.applied == [] and adapter.pos == [0.0, 0.0] and z(adapter) == 1.0


def test_unknown_workspace_fails_closed():
    b = Bridge(FakeJointAdapter(), Envelope(cfg(workspace=BOX)), org_id="org_demo", max_capability=Capability.CONTROL,
               clock=Clock())
    with pytest.raises(EnvelopeViolation) as e:
        b.propose(requested_by="employee:ops", target="joint_1", command="set_position_target", value=0.2, unit="rad",
                  expected_outcome=ExpectedOutcome("joint_1", "pos", 0.1, 0.3, "rad", 5.0))
    assert e.value.code == "workspace_unknown"


def test_starting_outside_the_box_is_refused():
    b, adapter = make(FakeArmAdapter(pos=(1.0, 0.0)))
    assert code_of(lambda: propose(b, value=0.9, lo=0.85, hi=0.95)) == "workspace"
    assert adapter.applied == []


def test_a_mid_path_excursion_is_refused_even_if_the_end_pose_is_fine():
    class Excursion(FakeArmAdapter):
        def dry_run(self, *a):
            real = super().dry_run(*a)
            return DryRun(real.final_pos, real.max_abs_vel, real.max_abs_force, real.reached_time,
                          per_joint=real.per_joint, site_min=(real.site_min[0], real.site_min[1], 0.1),
                          site_max=real.site_max)
    b, adapter = make(Excursion())
    p = approved(b)
    assert code_of(lambda: b.apply(p)) == "workspace" and adapter.applied == []


def test_dry_run_that_reports_no_positions_fails_closed():
    class Silent(FakeArmAdapter):
        def dry_run(self, *a):
            real = super().dry_run(*a)
            return DryRun(real.final_pos, real.max_abs_vel, real.max_abs_force, real.reached_time,
                          per_joint=real.per_joint)
    b, adapter = make(Silent())
    p = approved(b)
    assert code_of(lambda: b.apply(p)) == "workspace_unknown" and adapter.applied == []


def test_runtime_monitor_stops_a_real_motion_that_leaves_the_box_even_if_the_dry_run_lied():
    class Liar(FakeArmAdapter):
        def dry_run(self, *a):
            real = super().dry_run(*a)
            return DryRun(real.final_pos, real.max_abs_vel, real.max_abs_force, real.reached_time,
                          per_joint=real.per_joint, site_min=(0.5, 0.0, 0.9), site_max=(0.9, 0.0, 1.0))
    b, adapter = make(Liar())
    p = approved(b, value=1.0, lo=0.95, hi=1.05, by=8.0)
    assert code_of(lambda: b.apply(p)) == "workspace"
    assert adapter.stopped and adapter.pos[0] < 0.97 and z(adapter) < 0.4 + 0.25
    assert [e["event"] for e in b.audit][-2:] == ["rejected", "safe_stop"]


def test_another_joint_over_its_limit_is_caught_not_only_the_target():
    class Coupled(FakeArmAdapter):
        def dry_run(self, *a):
            real = super().dry_run(*a)
            pj = dict(real.per_joint)
            pj["joint_2"] = (99.0, 0.0)
            return DryRun(real.final_pos, real.max_abs_vel, real.max_abs_force, real.reached_time, per_joint=pj,
                          site_min=real.site_min, site_max=real.site_max)
    b, adapter = make(Coupled())
    p = approved(b)
    assert code_of(lambda: b.apply(p)) == "vel" and adapter.applied == []


def test_a_joint_missing_from_the_envelope_may_not_move():
    class Sneaky(FakeArmAdapter):
        def dry_run(self, *a):
            real = super().dry_run(*a)
            return DryRun(real.final_pos, real.max_abs_vel, real.max_abs_force, real.reached_time,
                          per_joint={"joint_9": (1.0, 1.0), **real.per_joint}, site_min=real.site_min, site_max=real.site_max)
    b, adapter = make(Sneaky())
    p = approved(b)
    assert code_of(lambda: b.apply(p)) == "joint_not_allowed" and adapter.applied == []


def test_approval_binding_still_holds_with_a_workspace():
    b, adapter = make()
    p = propose(b)
    with pytest.raises(ApprovalError):
        b.apply(p)
    assert adapter.applied == []
