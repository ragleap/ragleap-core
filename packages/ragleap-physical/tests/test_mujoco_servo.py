"""MuJoCo control behind the bridge (Phase 3). Runs only when MuJoCo is installed."""
import pytest

from ragleap_physical import (Bridge, Capability, Envelope, EnvelopeViolation, ApprovalError, SafetyStop)

from helpers import cfg, outcome, propose

LIMITS = {"joint_1": {"pos_min": -1.0, "pos_max": 1.0, "vel_max": 10.0, "force_max": 10.0}}


def build(**changes):
    pytest.importorskip("mujoco")
    from ragleap_physical.adapters.mujoco import MujocoServoAdapter
    config = cfg(targets=changes.pop("targets", LIMITS), **changes)
    adapter = MujocoServoAdapter()
    return Bridge(adapter, Envelope(config), org_id="org_demo", max_capability=Capability.CONTROL), adapter


def pos(adapter):
    return adapter.observe_raw()["entities"][0]["pos"]


def approved(bridge, **kw):
    p = propose(bridge, **kw)
    bridge.approve(p, "person:alice")
    return p


def test_full_path_on_real_engine_verifies_outcome():
    b, adapter = build()
    r = b.apply(approved(b))
    assert r.state == "verified" and r.outcome_ok and abs(pos(adapter) - 0.5) < 0.05


def test_same_command_gives_exactly_same_result():
    out = []
    for _ in range(2):
        b, adapter = build()
        b.apply(approved(b))
        out.append(float(pos(adapter)).hex())
    assert out[0] == out[1]


def test_dry_run_changes_nothing():
    _, adapter = build()
    before = adapter.observe_raw()
    pred = adapter.dry_run("joint_1", "set_position_target", 0.5, 2.0, 100000)
    assert adapter.observe_raw() == before and pred.reached_time and abs(pred.final_pos - 0.5) < 0.2


def test_force_limit_blocks_before_motion():
    b, adapter = build(targets={"joint_1": dict(LIMITS["joint_1"], force_max=0.5)})
    p = approved(b)
    with pytest.raises(EnvelopeViolation) as e:
        b.apply(p)
    assert e.value.code == "force" and pos(adapter) == 0.0


def test_speed_limit_blocks_before_motion():
    b, adapter = build(targets={"joint_1": dict(LIMITS["joint_1"], vel_max=0.01)})
    p = approved(b)
    with pytest.raises(EnvelopeViolation) as e:
        b.apply(p)
    assert e.value.code == "vel" and pos(adapter) == 0.0


def test_out_of_range_refused_before_any_engine_call():
    b, adapter = build()
    with pytest.raises(EnvelopeViolation):
        propose(b, value=2.0)
    assert pos(adapter) == 0.0


def test_missed_outcome_is_failed():
    b, _ = build()
    r = b.apply(approved(b, expected_outcome=outcome(lo=0.9, hi=1.0)))
    assert r.state == "failed" and not r.outcome_ok


def test_stop_holds_position_and_blocks_apply():
    b, adapter = build()
    p = approved(b)
    b.stop()
    with pytest.raises(SafetyStop):
        b.apply(p)
    adapter.advance(1.0, 100000)
    assert pos(adapter) == 0.0


def test_unknown_target_refused_by_adapter():
    _, adapter = build()
    with pytest.raises(ValueError):
        adapter.apply("joint_9", "set_position_target", 0.1)
    with pytest.raises(ValueError):
        adapter.apply("joint_1", "set_force", 0.1)


def test_no_approval_no_motion():
    b, adapter = build()
    p = propose(b)
    with pytest.raises(ApprovalError):
        b.apply(p)
    assert pos(adapter) == 0.0


def test_stop_holds_the_pose_it_stopped_at_even_after_clearing():
    """Stopping mid-motion must retarget the servo to where it is, not leave the old target in place."""
    _, adapter = build()
    adapter.apply("joint_1", "set_position_target", 0.5)
    adapter.advance(0.05, 100000)
    stopped_at = pos(adapter)
    assert 0.0 < stopped_at < 0.45
    adapter.stop()
    adapter.clear_stop()
    adapter.advance(4.0, 100000)
    assert abs(pos(adapter) - stopped_at) < 0.01
