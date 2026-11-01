"""Two-link arm on the real MuJoCo engine, with workspace limits. Runs only when MuJoCo is installed."""
import math

import pytest

from ragleap_physical import (Bridge, Capability, ConfigError, Envelope, EnvelopeViolation, ExpectedOutcome,
                              SafetyStop)

from helpers import Clock, cfg

JOINT = {"pos_min": -1.2, "pos_max": 1.2, "vel_max": 20.0, "force_max": 50.0}
BOX = {"x": [0.3, 1.0], "y": [-0.1, 0.1], "z": [0.4, 1.5]}


def build(targets=None, workspace=BOX):
    pytest.importorskip("mujoco")
    from ragleap_physical.adapters.mujoco import ARM2_XML, MujocoServoAdapter
    adapter = MujocoServoAdapter(ARM2_XML, model_id="arm2_v1")
    config = cfg(targets=targets or {"joint_1": dict(JOINT), "joint_2": dict(JOINT)}, workspace=workspace)
    return Bridge(adapter, Envelope(config), org_id="org_demo", max_capability=Capability.CONTROL, clock=Clock()), adapter


def propose(b, joint="joint_1", value=0.5, lo=0.45, hi=0.55, by=6.0):
    return b.propose(requested_by="employee:ops", target=joint, command="set_position_target", value=value, unit="rad",
                     expected_outcome=ExpectedOutcome(joint, "pos", lo, hi, "rad", by))


def approved(b, **kw):
    p = propose(b, **kw)
    b.approve(p, "person:alice")
    return p


def site(adapter):
    return {s["id"]: s["value"] for s in adapter.observe_raw()["sensors"]}


def test_observation_reports_the_end_effector_position():
    _, adapter = build()
    s = site(adapter)
    assert set(s) == {"ee_x", "ee_y", "ee_z"}
    assert abs(s["ee_x"] - 0.9) < 1e-6 and abs(s["ee_y"]) < 1e-6 and abs(s["ee_z"] - 1.0) < 1e-6


def test_forward_kinematics_matches_the_two_link_formula():
    b, adapter = build()
    b.apply(approved(b))
    j1 = adapter.observe_raw()["entities"][0]["pos"]
    s = site(adapter)
    assert abs(s["ee_x"] - (0.5 * math.cos(j1) + 0.4 * math.cos(j1))) < 1e-3
    assert abs(s["ee_z"] - (1.0 - 0.9 * math.sin(j1))) < 1e-3


def test_a_move_inside_the_box_is_verified_on_the_real_engine():
    b, adapter = build()
    r = b.apply(approved(b))
    s = site(adapter)
    assert r.state == "verified" and s["ee_z"] >= 0.4 and 0.3 <= s["ee_x"] <= 1.0


def test_end_pose_outside_the_box_is_refused_before_motion():
    b, adapter = build()
    p = approved(b, value=1.0, lo=0.95, hi=1.05)
    with pytest.raises(EnvelopeViolation) as e:
        b.apply(p)
    assert e.value.code == "workspace" and adapter.observe_raw()["entities"][0]["pos"] == 0.0


def test_same_command_gives_exactly_the_same_end_effector():
    out = []
    for _ in range(2):
        b, adapter = build()
        b.apply(approved(b))
        out.append(float(site(adapter)["ee_z"]).hex())
    assert out[0] == out[1]


def test_envelope_must_list_both_joints():
    pytest.importorskip("mujoco")
    from ragleap_physical.adapters.mujoco import ARM2_XML, MujocoServoAdapter
    with pytest.raises(ConfigError):
        Bridge(MujocoServoAdapter(ARM2_XML, model_id="arm2_v1"), Envelope(cfg(targets={"joint_1": dict(JOINT)}, workspace=BOX)),
               org_id="org_demo", max_capability=Capability.CONTROL)


def test_dry_run_reports_every_joint_and_the_path_extremes():
    _, adapter = build()
    pred = adapter.dry_run("joint_1", "set_position_target", 0.5, 4.0, 100000)
    assert set(pred.per_joint) == {"joint_1", "joint_2"} and pred.reached_time
    # Measured on MuJoCo 3.17.0: joint coupling lifts the tip about 0.16 mm above its start while joint 1 swings down.
    # That is why the whole path is tracked and not only the end pose. Allow 1 mm for it.
    assert pred.site_min[2] < 1.0 and pred.site_max[2] < 1.001 and pred.site_min[2] > 0.4


def test_dry_run_changes_nothing():
    _, adapter = build()
    before = adapter.observe_raw()
    adapter.dry_run("joint_2", "set_position_target", 0.5, 3.0, 100000)
    assert adapter.observe_raw() == before


def test_stop_holds_both_joints():
    b, adapter = build()
    adapter.apply("joint_1", "set_position_target", 0.5)
    adapter.advance(0.05, 100000)
    at = [e["pos"] for e in adapter.observe_raw()["entities"]]
    adapter.stop()
    adapter.clear_stop()
    adapter.advance(4.0, 100000)
    after = [e["pos"] for e in adapter.observe_raw()["entities"]]
    assert all(abs(a - b_) < 0.05 for a, b_ in zip(at, after))


def test_runtime_monitor_catches_motion_that_leaves_the_box():
    pytest.importorskip("mujoco")
    from ragleap_physical.adapters import DryRun
    from ragleap_physical.adapters.mujoco import ARM2_XML, MujocoServoAdapter

    class Liar(MujocoServoAdapter):
        def dry_run(self, *a):
            real = super().dry_run(*a)
            return DryRun(real.final_pos, real.max_abs_vel, real.max_abs_force, real.reached_time,
                          per_joint=real.per_joint, site_min=(0.5, 0.0, 0.9), site_max=(0.9, 0.0, 1.0))
    adapter = Liar(ARM2_XML, model_id="arm2_v1")
    b = Bridge(adapter, Envelope(cfg(targets={"joint_1": dict(JOINT), "joint_2": dict(JOINT)}, workspace=BOX)),
               org_id="org_demo", max_capability=Capability.CONTROL, clock=Clock())
    p = approved(b, value=1.0, lo=0.95, hi=1.05, by=8.0)
    with pytest.raises(EnvelopeViolation) as e:
        b.apply(p)
    assert e.value.code == "workspace" and adapter._stopped
