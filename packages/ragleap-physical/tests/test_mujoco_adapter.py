"""Runs only when MuJoCo is installed (the optional extra). Observe-only checks."""
import pytest

from ragleap_physical import Bridge, Capability, Envelope

from helpers import BASE


def adapter():
    pytest.importorskip("mujoco")
    from ragleap_physical.adapters.mujoco import MujocoAdapter
    return MujocoAdapter()


def test_observe_through_bridge_is_clean():
    mujoco = pytest.importorskip("mujoco")
    b = Bridge(adapter(), Envelope(BASE), org_id="org_demo", max_capability=Capability.OBSERVE)
    o = b.observe()
    assert o.dropped == 0 and o.simulated and o.source["engine_version"] == mujoco.__version__
    assert o.entity("joint_1").pos == 0.3 and o.entity("joint_1").pos_unit == "rad"


def test_model_hash_is_stable():
    a, b = adapter(), adapter()
    assert a.observe_raw()["source"]["model_sha256"] == b.observe_raw()["source"]["model_sha256"]
    assert len(a.observe_raw()["source"]["model_sha256"]) == 64


def test_advance_moves_the_pendulum_and_repeats_exactly():
    results = []
    for _ in range(2):
        a = adapter()
        assert a.advance(0.5, 100000) is True
        results.append(float(a.observe_raw()["entities"][0]["pos"]).hex())
    assert results[0] == results[1] and results[0] != float(0.3).hex()


def test_control_is_refused_until_phase_3():
    a = adapter()
    with pytest.raises(NotImplementedError):
        a.apply("joint_1", "set_position_target", 0.1)
    with pytest.raises(NotImplementedError):
        a.dry_run("joint_1", "set_position_target", 0.1, 1.0, 10)


def test_step_cap_is_respected():
    a = adapter()
    assert a.advance(10.0, 5) is False
