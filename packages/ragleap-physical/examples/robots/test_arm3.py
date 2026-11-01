"""Tests for the three-joint simulation-only arm."""
from pathlib import Path

import pytest

from ragleap_physical.adapters.fake import FakeArm3Adapter

XML = Path(__file__).with_name("arm3.xml")


def test_standin_three_joints_and_tip():
    a = FakeArm3Adapter()
    raw = a.observe_raw()
    assert a.targets() == ["joint_1", "joint_2", "joint_3"]
    assert len(raw["entities"]) == 3
    assert {s["id"]: s["value"] for s in raw["sensors"]} == pytest.approx({"ee_x": 0.9, "ee_y": 0.0, "ee_z": 1.0})


def test_standin_dry_run_is_immutable_and_tracks_all_joints():
    a = FakeArm3Adapter()
    before = a.observe_raw()
    prediction = a.dry_run("joint_3", "set_position_target", 0.5, 2.0, 300)
    assert prediction.reached_time
    assert set(prediction.per_joint) == set(a.targets())
    assert prediction.site_min[2] < 1.0 <= prediction.site_max[2]
    assert a.observe_raw() == before


def test_standin_rejects_unknown_command_without_motion():
    a = FakeArm3Adapter()
    with pytest.raises(ValueError):
        a.dry_run("joint_3", "change_tool", 0.5, 2.0, 300)
    assert a.applied == []


def test_mujoco_three_joints_and_tip():
    pytest.importorskip("mujoco")
    from ragleap_physical.adapters.mujoco import MujocoServoAdapter
    a = MujocoServoAdapter(XML.read_text(), model_id="arm3_v1")
    assert set(a.targets()) == {"joint_1", "joint_2", "joint_3"}
    raw = a.observe_raw()
    assert len(raw["entities"]) == 3
    sensors = {s["id"]: s["value"] for s in raw["sensors"]}
    assert sensors["ee_x"] == pytest.approx(0.9)
    assert sensors["ee_z"] == pytest.approx(1.0)
    before = a.observe_raw()
    pred = a.dry_run("joint_3", "set_position_target", 0.3, 0.5, 1000)
    assert pred.reached_time and set(pred.per_joint) == set(a.targets())
    assert a.observe_raw() == before


def test_standin_rejects_wrong_position_count():
    with pytest.raises(ValueError, match="three joint positions required"):
        FakeArm3Adapter(pos=(0.0, 0.0))
