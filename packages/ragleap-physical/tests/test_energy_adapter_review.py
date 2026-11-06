"""Independent review tests for the energy-site adapter.

Six cases found in review: bad `advance()` input (fractional or boolean step count, infinite target) and
non-finite numbers (extreme step length or meter power). They are kept in their own file, apart from
test_energy_adapter.py, so they stay as regression tests.
"""
import math

import pytest

from ragleap_physical.adapters.energy import (
    MAX_METERS,
    MAX_POWER_W,
    MAX_STEP_S,
    FakeEnergySiteAdapter,
    MeterSpec,
)


def test_review_fractional_max_steps_is_rejected_without_advancing():
    adapter = FakeEnergySiteAdapter()
    with pytest.raises(ValueError):
        adapter.advance(120.0, 1.5)
    assert adapter.t == 0.0
    assert adapter.steps == 0


def test_review_boolean_max_steps_is_rejected_without_advancing():
    adapter = FakeEnergySiteAdapter()
    with pytest.raises(ValueError):
        adapter.advance(120.0, True)
    assert adapter.t == 0.0
    assert adapter.steps == 0


def test_review_infinite_target_is_rejected_without_advancing():
    adapter = FakeEnergySiteAdapter()
    with pytest.raises(ValueError):
        adapter.advance(float("inf"), 10)
    assert adapter.t == 0.0
    assert adapter.steps == 0


def test_review_extreme_step_duration_is_rejected_before_telemetry_can_overflow():
    with pytest.raises(ValueError):
        FakeEnergySiteAdapter(step_s=1.0e308)
    # The documented upper boundary itself remains valid.
    adapter = FakeEnergySiteAdapter(
        step_s=MAX_STEP_S,
        meters=[MeterSpec("boundary", MAX_POWER_W, MAX_POWER_W)],
    )
    assert adapter.step_s == MAX_STEP_S


def test_review_extreme_meter_power_is_rejected_before_energy_can_overflow():
    with pytest.raises(ValueError):
        FakeEnergySiteAdapter(meters=[MeterSpec("overflow", 1.0e308, 1.0)])
    with pytest.raises(ValueError):
        FakeEnergySiteAdapter(meters=[MeterSpec("overflow", 1.0, 1.0e308)])
    # The documented power boundary itself remains valid.
    adapter = FakeEnergySiteAdapter(
        meters=[MeterSpec("boundary", MAX_POWER_W, MAX_POWER_W)]
    )
    assert adapter.meters[0].run_w == MAX_POWER_W


def test_review_site_energy_and_totals_remain_finite_at_all_documented_limits():
    meters = [
        MeterSpec(f"meter_{i}", MAX_POWER_W, MAX_POWER_W)
        for i in range(MAX_METERS)
    ]
    adapter = FakeEnergySiteAdapter(
        seed=7,
        meters=meters,
        step_s=MAX_STEP_S,
        noise=0.49,
        shift_start_h=0.0,
        shift_end_h=24.0,
    )

    assert adapter.advance(MAX_STEP_S * 2000, 2000) is True
    raw = adapter.observe_raw()

    assert math.isfinite(raw["sim_time_s"])
    values = [sensor["value"] for sensor in raw["sensors"]]
    assert all(math.isfinite(value) for value in values)

    by_id = {sensor["id"]: sensor["value"] for sensor in raw["sensors"]}
    assert math.isfinite(by_id["site_power"])
    assert math.isfinite(by_id["site_energy"])
    assert math.isclose(
        by_id["site_power"],
        sum(by_id[f"power_{meter.id}"] for meter in meters),
        rel_tol=1e-12,
    )
    assert math.isclose(
        by_id["site_energy"],
        sum(by_id[f"energy_{meter.id}"] for meter in meters),
        rel_tol=1e-12,
    )
