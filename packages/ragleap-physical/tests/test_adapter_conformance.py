"""Cross-adapter conformance checks for ragleap-physical 0.0.4+.

Contract:
* Every adapter reports simulated=True, exposes the common observation shape,
  and has a stable entity count before stop, after stop, and after clear_stop.
* stop() and clear_stop() must not raise. A Bridge over each adapter must also
  complete stop() followed by clear_stop().
* dry_run() must not change observe_raw(). A refused apply() must not change it.
* Observe-only adapters must refuse apply() and dry_run() (never return);
  ValueError, PermissionError, and NotImplementedError are accepted refusals.
* Control adapters must raise ValueError for malformed values, unknown targets,
  and unknown commands. TypeError is not a valid refusal.

Place this file in the package tests/ directory. It is a test-only module.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import copy

import pytest

from ragleap_physical import Bridge, Capability, Envelope
from ragleap_physical.adapters.energy import FakeEnergySiteAdapter
from ragleap_physical.adapters.fake import FakeArm3Adapter, FakeArmAdapter, FakeJointAdapter
from ragleap_physical.adapters.machine import FakeMachineToolAdapter
from ragleap_physical.adapters.mtconnect import FakeMTConnectAdapter, Item
from ragleap_physical.adapters.ros2 import MockROS2ObserveAdapter
from ragleap_physical.adapters.mujoco import MujocoAdapter, MujocoServoAdapter, ARM2_XML


def _mtconnect():
    document = (
        '<?xml version="1.0"?><MTConnectStreams>'
        '<Header creationTime="2026-10-28T10:00:00.000Z" sender="conformance"/>'
        '<Streams><DeviceStream name="fixture"><ComponentStream component="Path">'
        '<Samples><Position dataItemId="Xpos" sequence="1">0.25</Position></Samples>'
        '</ComponentStream></DeviceStream></Streams></MTConnectStreams>'
    )
    return FakeMTConnectAdapter([document], {"Xpos": Item("m")})


def _mujoco_arm3():
    xml = (Path(__file__).resolve().parents[1] / "examples" / "robots" / "arm3.xml").read_text(encoding="utf-8")
    return MujocoServoAdapter(xml, model_id="arm3_v1")


@dataclass(frozen=True)
class AdapterCase:
    name: str
    factory: Callable
    mode: str  # "control" or "observe_only"
    target: str | None = None
    command: str | None = None
    value: float = 0.1
    targets: tuple[str, ...] | None = ()


CASES = (
    AdapterCase("fake_one_joint", FakeJointAdapter, "control", "joint_1", "set_position_target",
                targets=("joint_1",)),
    AdapterCase("fake_two_link_arm", FakeArmAdapter, "control", "joint_1", "set_position_target",
                targets=("joint_1", "joint_2")),
    AdapterCase("fake_three_joint_arm", FakeArm3Adapter, "control", "joint_1", "set_position_target",
                targets=("joint_1", "joint_2", "joint_3")),
    AdapterCase("fake_machine_tool", FakeMachineToolAdapter, "control", "tool_1", "set_tool_offset",
                value=0.5, targets=("tool_1",)),
    AdapterCase("ros2_mock", MockROS2ObserveAdapter, "observe_only", targets=()),
    AdapterCase("energy_mock", FakeEnergySiteAdapter, "observe_only", targets=()),
    AdapterCase("mtconnect_mock", _mtconnect, "observe_only", targets=()),
    AdapterCase("mujoco_observe_only_pendulum", MujocoAdapter, "observe_only", targets=None),
    AdapterCase("mujoco_servo_one_joint", MujocoServoAdapter, "control", "joint_1",
                "set_position_target", targets=("joint_1",)),
    AdapterCase("mujoco_servo_two_link_arm", lambda: MujocoServoAdapter(ARM2_XML, model_id="arm2_v1"),
                "control", "joint_1", "set_position_target", targets=("joint_1", "joint_2")),
    AdapterCase("mujoco_servo_three_joint_arm", _mujoco_arm3, "control", "joint_1",
                "set_position_target", targets=("joint_1", "joint_2", "joint_3")),
)


@pytest.fixture(params=CASES, ids=lambda case: case.name)
def adapter_case(request):
    case = request.param
    try:
        adapter = case.factory()
    except ImportError as exc:
        pytest.skip(f"optional dependency unavailable: {exc}")
    return case, adapter


def _expected_refusal(case: AdapterCase):
    return ValueError if case.mode == "control" else (ValueError, PermissionError, NotImplementedError)


def _joint_limits():
    return {"pos_min": -3.1, "pos_max": 3.1, "vel_max": 10.0, "force_max": 10.0}


def _bridge_for(case: AdapterCase, adapter):
    if case.mode == "observe_only":
        return Bridge(adapter, Envelope.observe_only(), org_id="org_demo", max_capability=Capability.ANALYSE)
    if case.command == "set_tool_offset":
        limits = {"offset_min": -60.0, "offset_max": 60.0, "step_max": 4.0, "min_gap_s": 0.0}
        commands = ["set_tool_offset"]
    else:
        limits = _joint_limits()
        commands = ["set_position_target"]
    config = {
        "envelope_version": 0,
        "allowed_commands": commands,
        "targets": {target: dict(limits) for target in (adapter.targets() or ())},
        "rate": {"max_commands_per_s": 5},
        "episode": {"max_sim_time_s": 100000.0, "max_steps": 1000000},
        "on_violation": "reject",
    }
    return Bridge(adapter, Envelope(config), org_id="org_demo", max_capability=Capability.CONTROL)


def test_simulated_is_true(adapter_case):
    _, adapter = adapter_case
    assert adapter.simulated is True


def test_observe_returns_the_common_raw_shape(adapter_case):
    _, adapter = adapter_case
    raw = adapter.observe_raw()
    assert isinstance(raw, dict)
    assert isinstance(raw.get("source"), dict)
    assert isinstance(raw.get("sim_time_s"), (int, float))
    assert isinstance(raw.get("step"), int)
    assert isinstance(raw.get("entities"), list)
    assert isinstance(raw.get("sensors"), list)


def test_targets_match_the_declared_capability(adapter_case):
    case, adapter = adapter_case
    actual = adapter.targets()
    if case.targets is None:
        assert actual is None
    else:
        assert isinstance(actual, list)
        assert tuple(actual) == case.targets


def test_dry_run_is_implemented_or_refused_read_only(adapter_case):
    case, adapter = adapter_case
    before = copy.deepcopy(adapter.observe_raw())
    if case.mode == "observe_only":
        with pytest.raises(_expected_refusal(case)):
            adapter.dry_run("not_a_target", "not_a_command", 0.1, 0.02, 10)
    else:
        result = adapter.dry_run(case.target, case.command, case.value, 0.02, 10)
        assert hasattr(result, "reached_time")
        assert hasattr(result, "final_pos")
    assert adapter.observe_raw() == before


def test_apply_is_implemented_or_refused_read_only(adapter_case):
    case, adapter = adapter_case
    if case.mode == "observe_only":
        with pytest.raises(_expected_refusal(case)):
            adapter.apply("not_a_target", "not_a_command", 0.1)
    else:
        adapter.apply(case.target, case.command, case.value)


def test_stop_and_clear_stop_are_available(adapter_case):
    _, adapter = adapter_case
    assert adapter.stop() is None
    assert adapter.clear_stop() is None


def test_entity_count_is_stable_across_stop_and_clear_stop(adapter_case):
    _, adapter = adapter_case
    before = len(adapter.observe_raw()["entities"])
    adapter.stop()
    stopped = len(adapter.observe_raw()["entities"])
    adapter.clear_stop()
    cleared = len(adapter.observe_raw()["entities"])
    assert stopped == before
    assert cleared == before


def test_dry_run_does_not_change_observation(adapter_case):
    case, adapter = adapter_case
    before = copy.deepcopy(adapter.observe_raw())
    if case.mode == "observe_only":
        with pytest.raises(_expected_refusal(case)):
            adapter.dry_run("not_a_target", "not_a_command", 0.1, 0.02, 10)
    else:
        adapter.dry_run(case.target, case.command, case.value, 0.02, 10)
    assert adapter.observe_raw() == before


def test_refused_apply_leaves_observation_unchanged(adapter_case):
    case, adapter = adapter_case
    before = copy.deepcopy(adapter.observe_raw())
    if case.mode == "observe_only":
        call = lambda: adapter.apply("not_a_target", "not_a_command", 0.1)
    else:
        call = lambda: adapter.apply(case.target, case.command, float("nan"))
    with pytest.raises(_expected_refusal(case)):
        call()
    assert adapter.observe_raw() == before


def test_bridge_can_stop_and_clear_for_every_adapter(adapter_case):
    case, adapter = adapter_case
    bridge = _bridge_for(case, adapter)
    bridge.stop()
    bridge.clear_stop()
    assert len(bridge.observe().entities) == len(adapter.observe_raw()["entities"])


def test_ros2_bridge_can_be_cleared_after_stop():
    adapter = MockROS2ObserveAdapter()
    bridge = Bridge(adapter, Envelope.observe_only(), org_id="org_demo", max_capability=Capability.ANALYSE)
    bridge.stop()
    bridge.clear_stop()
    assert len(bridge.observe().entities) == 1


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), "0.1", True, None],
                         ids=["nan", "infinity", "text", "boolean", "none"])
def test_apply_refuses_bad_input(adapter_case, bad_value):
    case, adapter = adapter_case
    with pytest.raises(_expected_refusal(case)):
        adapter.apply(case.target or "not_a_target", case.command or "not_a_command", bad_value)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), "0.1", True, None],
                         ids=["nan", "infinity", "text", "boolean", "none"])
def test_dry_run_refuses_bad_input(adapter_case, bad_value):
    case, adapter = adapter_case
    with pytest.raises(_expected_refusal(case)):
        adapter.dry_run(case.target or "not_a_target", case.command or "not_a_command",
                        bad_value, 0.02, 10)


@pytest.mark.parametrize("operation", ["apply", "dry_run"], ids=["apply", "dry_run"])
@pytest.mark.parametrize("invalid_part", ["target", "command"], ids=["unknown_target", "unknown_command"])
def test_control_adapter_rejects_unknown_target_or_command(adapter_case, operation, invalid_part):
    case, adapter = adapter_case
    if case.mode != "control":
        pytest.skip("unknown command/target refusal is specified for control adapters")
    target = "not_a_target" if invalid_part == "target" else case.target
    command = "not_a_command" if invalid_part == "command" else case.command
    if operation == "apply":
        with pytest.raises(ValueError):
            adapter.apply(target, command, case.value)
    else:
        with pytest.raises(ValueError):
            adapter.dry_run(target, command, case.value, 0.02, 10)
