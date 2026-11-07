"""Review tests for arm3.xml with the real MuJoCo servo adapter (they skip when MuJoCo is not installed).

A target beyond the actuator range must be refused, never silently clamped, and the adapter must refuse values that
are not finite numbers.
"""
from pathlib import Path

import pytest


def arm3_xml():
    root = Path(__file__).resolve().parents[1]
    found = sorted(root.rglob("arm3.xml"))
    assert found, "arm3.xml is not in the package"
    return found[0].read_text()


def servo():
    pytest.importorskip("mujoco")
    from ragleap_physical.adapters.mujoco import MujocoServoAdapter
    return MujocoServoAdapter(arm3_xml(), model_id="arm3_v1")


def test_arm3_mujoco_review_m1_a_target_beyond_the_actuator_range_is_refused_not_silently_clamped():
    """Risk: a model actuator range narrower than the envelope would silently clamp a target that the envelope allows (1.5 rad
    became 1.2 rad): a second, hidden limit outside the envelope. arm3.xml now uses -3.1 3.1 like the other models."""
    a = servo()
    try:
        a.apply("joint_1", "set_position_target", 1.5)
    except ValueError:
        return  # refusing the value is a good answer
    a.advance(8.0, 100000)
    pos = {e["id"]: e["pos"] for e in a.observe_raw()["entities"]}["joint_1"]
    assert pos > 1.4, f"joint_1 stopped at {pos:.3f} rad after a command of 1.5 rad"


def test_arm3_mujoco_review_m2_the_mujoco_servo_adapter_refuses_values_that_are_not_finite():
    """Risk: NaN, inf, text, bool and None must not reach MuJoCo, which answers a NaN control with a warning and a reset
    of the simulation state."""
    a = servo()
    for bad in (float("nan"), float("inf"), float("-inf"), "0.5", True, None):
        with pytest.raises(ValueError):
            a.apply("joint_1", "set_position_target", bad)
        with pytest.raises(ValueError):
            a.dry_run("joint_1", "set_position_target", bad, 0.5, 100)
