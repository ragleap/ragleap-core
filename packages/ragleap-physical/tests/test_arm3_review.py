"""Review tests for the three-joint stand-in arm (FakeArm3Adapter).

Each test states one risk found in review: the arm must stay observable and usable after a stop, refuse values and
inputs that are not finite numbers, refuse meaningless constructor parameters, and fingerprint its own parameters.
They are kept as regression tests. Not covered here: a command accepted while the arm is stopped (the bridge refuses it).
"""
import re
from pathlib import Path

import pytest

from ragleap_physical import Bridge, Capability, Envelope
from ragleap_physical.adapters.fake import FakeArm3Adapter

from helpers import Clock, cfg

JOINT = {"pos_min": -1.2, "pos_max": 1.2, "vel_max": 10.0, "force_max": 10.0}
NOT_A_FINITE_NUMBER = (float("nan"), float("inf"), float("-inf"), "0.5", True, None)


def arm3_cfg():
    return cfg(targets={f"joint_{i}": dict(JOINT) for i in (1, 2, 3)})


def test_arm3_review_1_the_arm_can_still_be_observed_after_a_stop():
    """Risk: FakeArmAdapter.stop() resets the velocities to two numbers, so on the three-joint arm observe_raw() raises
    IndexError right after a stop and the safe state can no longer be read or verified."""
    a = FakeArm3Adapter()
    a.stop()
    assert [e["vel"] for e in a.observe_raw()["entities"]] == [0.0, 0.0, 0.0]


def test_arm3_review_2_the_arm_works_again_after_stop_and_clear_stop():
    """Risk: after stop() and clear_stop() the next apply/advance/dry_run raises IndexError (same two-number velocity
    list), so the operator cannot recover from a stop without rebuilding the adapter."""
    a = FakeArm3Adapter()
    a.stop()
    a.clear_stop()
    a.apply("joint_1", "set_position_target", 0.3)
    a.advance(0.1, 100)
    assert a.dry_run("joint_2", "set_position_target", 0.2, 0.2, 100).reached_time


def test_arm3_review_3_the_bridge_can_observe_after_a_stop():
    """Risk: every bridge call ends with observe(), so after Bridge.stop() on this arm every later call fails with
    IndexError, not only the adapter's own."""
    b = Bridge(FakeArm3Adapter(), Envelope(arm3_cfg()), org_id="org_demo", max_capability=Capability.CONTROL, clock=Clock())
    b.stop()
    assert len(b.observe().entities) == 3


def test_arm3_review_4_apply_refuses_values_that_are_not_finite_numbers():
    """Risk: apply() stores NaN, text or a bool as the target; with NaN the PD law saturates to the maximum acceleration,
    so joint_1 ran away to 72 rad and 24 rad/s within 7 s, and with text the next advance() crashes."""
    for bad in NOT_A_FINITE_NUMBER:
        a = FakeArm3Adapter()
        with pytest.raises(ValueError):
            a.apply("joint_1", "set_position_target", bad)
        assert a.target == [0.0, 0.0, 0.0], repr(bad)


def test_arm3_review_5_dry_run_refuses_values_that_are_not_finite_numbers():
    """Risk: dry_run() converts the value with float() and returns a normal-looking prediction (NaN gave a final position
    of 0.51 rad, text '0.5' was accepted), so an invalid command can pass the limit check on a made-up trajectory."""
    for bad in NOT_A_FINITE_NUMBER:
        with pytest.raises(ValueError):
            FakeArm3Adapter().dry_run("joint_1", "set_position_target", bad, 0.5, 100)


def test_arm3_review_6_time_targets_and_step_limits_are_checked():
    """Risk: advance() and dry_run() give normal answers for NaN, negative and fractional inputs (a negative target says
    'reached', NaN says 'not reached'), text raises TypeError, and an infinite target with an infinite step limit never ends."""
    for bad_time in (float("nan"), -1.0, True, "1"):
        with pytest.raises(ValueError):
            FakeArm3Adapter().advance(bad_time, 10)
    for bad_steps in (-5, 2.5, True, None):
        with pytest.raises(ValueError):
            FakeArm3Adapter().advance(1.0, bad_steps)
    for until, steps in ((float("nan"), 10), (-1.0, 10), (1.0, -3), (1.0, 2.5)):
        with pytest.raises(ValueError):
            FakeArm3Adapter().dry_run("joint_1", "set_position_target", 0.3, until, steps)


def test_arm3_review_7_the_constructor_refuses_meaningless_parameters():
    """Risk: dt=0 stops time, dt=-0.01 gives a negative simulated time (-0.5 s after advance), NaN gains give NaN physics,
    and pos='abc' passes the length check and stores letters as joint angles."""
    for kw in (dict(dt=0.0), dict(dt=-0.01), dict(dt=float("nan")), dict(kp=float("nan")), dict(kd=float("inf")),
               dict(amax=-1.0), dict(pos=(float("nan"), 0.0, 0.0)), dict(pos="abc"), dict(pos=(0.0, 0.0, "x"))):
        with pytest.raises(ValueError):
            FakeArm3Adapter(**kw)


def test_arm3_review_8_the_model_fingerprint_identifies_the_parameters():
    """Risk (low): model_sha256 is the same placeholder for every parameter set, so the audit trail cannot tell a run with
    other gains or another time step from a run with the defaults."""
    ids = {FakeArm3Adapter(**kw).observe_raw()["source"]["model_sha256"] for kw in (dict(), dict(kp=9.0), dict(dt=0.005))}
    assert len(ids) == 3


def test_arm3_review_9_the_default_pytest_run_collects_the_arm3_tests():
    """Risk: the project's pytest settings collect only the listed testpaths; examples/robots/test_arm3.py is outside them,
    so a plain 'pytest' never runs the three-joint arm tests and a failing arm would go unnoticed in CI."""
    root = Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    testpaths = re.findall(r'"([^"]+)"', re.search(r"testpaths\s*=\s*\[(.*?)\]", text, re.S).group(1))
    found = [p.relative_to(root) for p in root.rglob("test_arm3.py")]
    assert found, "test_arm3.py is not in the package"
    assert any(f.parts[:len(Path(tp).parts)] == Path(tp).parts for f in found for tp in testpaths), (found, testpaths)
