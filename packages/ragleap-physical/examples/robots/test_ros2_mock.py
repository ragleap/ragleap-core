"""Offline-only checks. No ROS installation or network needed."""
import pytest

from ragleap_physical.adapters.ros2 import MockROS2ObserveAdapter
from ragleap_physical.observation import Observation


def test_fixture_observation_sanitizes():
    adapter = MockROS2ObserveAdapter(seed=7)
    adapter.ingest_fixture(position=0.25, sim_time_s=0.1, step=1)
    obs = Observation.from_raw(adapter.observe_raw(), org_id="demo", observation_id="obs1")
    assert obs.simulated and obs.entities[0].pos == 0.25
    assert adapter.targets() == []


@pytest.mark.parametrize("method,args", [
    ("apply", ("joint_1", "set_position_target", 1.0)),
    ("dry_run", ("joint_1", "set_position_target", 1.0, 1.0, 10)),
    ("advance", (1.0, 10)),
    ("clear_stop", ()),
])
def test_no_motion_interface(method, args):
    adapter = MockROS2ObserveAdapter()
    with pytest.raises(PermissionError, match="observe-only"):
        getattr(adapter, method)(*args)


def test_bad_fixture_does_not_change_state():
    adapter = MockROS2ObserveAdapter()
    before = adapter.observe_raw()
    with pytest.raises(ValueError):
        adapter.ingest_fixture(position=float("nan"), sim_time_s=0.1, step=1)
    assert adapter.observe_raw() == before
    adapter.ingest_fixture(position=0.2, sim_time_s=0.2, step=2)
    before = adapter.observe_raw()
    with pytest.raises(ValueError):
        adapter.ingest_fixture(position=0.3, sim_time_s=0.1, step=3)
    assert adapter.observe_raw() == before


def test_stop_does_not_change_observation():
    adapter = MockROS2ObserveAdapter()
    before = adapter.observe_raw()
    adapter.stop()
    assert adapter.observe_raw() == before


def test_fixture_rejects_regressing_step_with_increasing_time():
    adapter = MockROS2ObserveAdapter()
    adapter.ingest_fixture(position=0.2, sim_time_s=1.0, step=5)
    before = adapter.observe_raw()
    with pytest.raises(ValueError, match="regressing fixture step"):
        adapter.ingest_fixture(position=0.3, sim_time_s=2.0, step=4)
    assert adapter.observe_raw() == before
