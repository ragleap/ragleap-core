"""Regression tests for the three-joint arm and the MuJoCo servo adapter."""

from pathlib import Path

import pytest

from ragleap_physical.adapters.fake import FakeArm3Adapter


def test_stop_preserves_three_joint_observation_and_clear_stop():
    adapter = FakeArm3Adapter()
    adapter.stop()
    raw = adapter.observe_raw()
    assert len(raw["entities"]) == 3
    assert [entity["vel"] for entity in raw["entities"]] == [0.0, 0.0, 0.0]
    adapter.clear_stop()
    assert len(adapter.observe_raw()["entities"]) == 3


def test_fake_arm3_fingerprint_tracks_controller_parameters():
    default = FakeArm3Adapter().observe_raw()["source"]["model_sha256"]
    assert FakeArm3Adapter(kp=9.0).observe_raw()["source"]["model_sha256"] != default
    assert FakeArm3Adapter(dt=0.005).observe_raw()["source"]["model_sha256"] != default


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "0.5", True, None])
def test_mujoco_servo_rejects_invalid_values_without_mutating_control(bad):
    pytest.importorskip("mujoco")
    from ragleap_physical.adapters.mujoco import MujocoServoAdapter

    adapter = MujocoServoAdapter()
    before = adapter._d.ctrl.copy()
    with pytest.raises(ValueError):
        adapter.apply("joint_1", "set_position_target", bad)
    assert list(adapter._d.ctrl) == list(before)
    with pytest.raises(ValueError):
        adapter.dry_run("joint_1", "set_position_target", bad, 0.5, 100)


def test_mujoco_servo_rejects_targets_outside_actuator_ctrlrange():
    pytest.importorskip("mujoco")
    from ragleap_physical.adapters.mujoco import MujocoServoAdapter

    xml = Path(__file__).with_name("arm3.xml").read_text(encoding="utf-8")
    adapter = MujocoServoAdapter(xml, model_id="arm3_v1")
    before = adapter._d.ctrl.copy()
    with pytest.raises(ValueError, match="ctrlrange"):
        adapter.apply("joint_1", "set_position_target", 3.2)
    assert list(adapter._d.ctrl) == list(before)
    with pytest.raises(ValueError, match="ctrlrange"):
        adapter.dry_run("joint_1", "set_position_target", 3.2, 0.5, 100)
