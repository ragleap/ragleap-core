"""A simulated energy site: observe only, sensors only, seeded. Invented numbers, no real meter, no network."""
import math

import pytest

from ragleap_physical import Bridge, Capability, CapabilityError, Envelope, Observation, PhysicalError
from ragleap_physical.adapters.energy import (DEFAULT_METERS, MAX_METERS, MAX_POWER_W, MAX_STEP_S, FakeEnergySiteAdapter,
                                              MeterSpec)
from ragleap_physical.observation import UNITS

from helpers import Clock, cfg, propose

N_DEFAULT_SENSORS = 3 + 2 * len(DEFAULT_METERS)
# W and kWh are approved but only A can add them to observation.UNITS. Until then the core drops those sensors.
CORE_HAS_ENERGY_UNITS = {"W", "kWh"} <= UNITS


def sensors(adapter):
    return {s["id"]: s["value"] for s in adapter.observe_raw()["sensors"]}


def quiet(**kw):
    kw.setdefault("noise", 0.0)
    return FakeEnergySiteAdapter(**kw)


def rejects(**kw):
    with pytest.raises(ValueError):
        FakeEnergySiteAdapter(**kw)


# ---- shape and units
def test_first_observation_has_the_expected_sensors_and_no_entities():
    a = FakeEnergySiteAdapter(seed=4)
    raw = a.observe_raw()
    assert raw["entities"] == [] and len(raw["sensors"]) == N_DEFAULT_SENSORS
    assert raw["sim_time_s"] == 0.0 and raw["step"] == 0 and raw["seed"] == 4
    values = sensors(a)
    assert values["shift_active"] == 0.0 and values["site_energy"] == 0.0
    assert all(values[f"energy_{m.id}"] == 0.0 and values[f"power_{m.id}"] == m.idle_w for m in DEFAULT_METERS)
    assert {s["unit"] for s in raw["sensors"]} == {"W", "kWh", "1"}
    assert a.simulated is True


def test_site_totals_are_the_sum_of_the_meters():
    a = FakeEnergySiteAdapter(seed=2)
    a.advance(7200.0, 10000)
    v = sensors(a)
    assert math.isclose(v["site_power"], sum(v[f"power_{m.id}"] for m in DEFAULT_METERS), rel_tol=1e-12)
    assert math.isclose(v["site_energy"], sum(v[f"energy_{m.id}"] for m in DEFAULT_METERS), rel_tol=1e-12)
    assert v["site_power"] > 0 and v["site_energy"] > 0


def test_the_core_accepts_the_observation_and_drops_only_units_it_does_not_list_yet():
    a = FakeEnergySiteAdapter(seed=1)
    obs = Observation.from_raw(a.observe_raw(), org_id="org_demo", observation_id="obs_000001")
    assert obs.simulated is True and obs.trust == "untrusted" and obs.entities == () and not obs.truncated
    assert len(obs.sensors) + obs.dropped == N_DEFAULT_SENSORS
    if CORE_HAS_ENERGY_UNITS:
        assert obs.dropped == 0
    else:  # known gap, waiting for A: every W and kWh sensor is dropped and counted
        assert obs.dropped == N_DEFAULT_SENSORS - 1 and [s.id for s in obs.sensors] == ["shift_active"]


def test_the_largest_allowed_site_fits_in_one_observation():
    meters = [MeterSpec("m" * 50 if i == 0 else f"meter_{i}", 100.0, 10.0) for i in range(MAX_METERS)]
    a = FakeEnergySiteAdapter(meters=meters)
    obs = Observation.from_raw(a.observe_raw(), org_id="org_demo", observation_id="obs_000001")
    assert len(obs.sensors) + obs.dropped == 3 + 2 * MAX_METERS and not obs.truncated


# ---- the model
def test_same_seed_same_numbers_different_seed_different_numbers():
    def run(seed):
        a = FakeEnergySiteAdapter(seed=seed)
        a.advance(3600.0, 10000)
        return a.observe_raw()["sensors"]

    assert run(7) == run(7)
    assert run(7) != run(8)


def test_energy_is_power_times_time():
    a = quiet(shift_start_h=0.0, shift_end_h=24.0)  # always in shift, no noise
    assert a.advance(3600.0, 10000)
    v = sensors(a)
    assert math.isclose(v["energy_line_1"], 18.0, rel_tol=1e-9)  # 18000 W for one hour is 18 kWh
    assert math.isclose(v["energy_compressor"], 7.5, rel_tol=1e-9)
    assert math.isclose(v["power_hvac"], 5000.0, rel_tol=1e-12)


def test_power_follows_the_shift_and_the_day_repeats():
    a = quiet(step_s=3600.0)
    a.advance(5 * 3600.0, 100)
    assert sensors(a)["shift_active"] == 0.0 and sensors(a)["power_line_1"] == 400.0
    a.advance(6 * 3600.0, 100)  # 06:00 starts the shift
    assert sensors(a)["shift_active"] == 1.0 and sensors(a)["power_line_1"] == 18000.0
    a.advance(22 * 3600.0, 100)  # 22:00 ends it
    assert sensors(a)["shift_active"] == 0.0 and sensors(a)["power_line_1"] == 400.0
    a.advance(29 * 3600.0, 100)  # 05:00 the next day
    assert sensors(a)["shift_active"] == 0.0
    a.advance(30 * 3600.0, 100)  # 06:00 the next day
    assert sensors(a)["shift_active"] == 1.0


def test_power_is_never_negative_and_energy_never_goes_down():
    a = FakeEnergySiteAdapter(seed=3, noise=0.49)  # large noise: a raw sample would often be below zero
    last = sensors(a)
    for _ in range(2000):
        a.advance(a.t + a.step_s, 1)
        now = sensors(a)
        assert all(v >= 0.0 for v in now.values())
        assert all(now[k] >= last[k] for k in now if k.startswith("energy_") or k == "site_energy")
        last = now


def test_the_model_fingerprint_follows_the_model_not_the_seed():
    sha = lambda **kw: FakeEnergySiteAdapter(**kw).observe_raw()["source"]["model_sha256"]
    assert len(sha()) == 64 and sha(seed=1) == sha(seed=2)
    assert sha() != sha(step_s=30.0) and sha() != sha(noise=0.05) and sha() != sha(shift_start_h=7.0)
    assert sha() != sha(shift_end_h=21.0)
    assert sha() != sha(meters=[MeterSpec("line_1", 18001.0, 400.0)] + list(DEFAULT_METERS[1:]))


# ---- time control
def test_advance_stops_at_max_steps_and_while_stopped():
    a = FakeEnergySiteAdapter()
    assert a.advance(1.0e9, 5) is False and a.steps == 5 and a.t == 5 * a.step_s
    a.stop()
    assert a.advance(1.0e9, 5) is False and a.steps == 5
    a.clear_stop()
    assert a.advance(a.t + 2 * a.step_s, 5) is True and a.steps == 7


# ---- observe only
def test_there_is_no_command_and_nothing_is_a_target():
    a = FakeEnergySiteAdapter()
    assert a.targets() == []
    for call in (lambda: a.apply("meter", "set_power", 1.0), lambda: a.dry_run("meter", "set_power", 1.0, 10.0, 100)):
        with pytest.raises(ValueError):
            call()
    assert a.t == 0.0 and a.steps == 0 and sum(sensors(a)[k] for k in sensors(a) if k.startswith("energy_")) == 0.0


def test_an_observe_bridge_reads_but_never_proposes():
    a = FakeEnergySiteAdapter()
    b = Bridge(a, Envelope(cfg()), org_id="org_demo", max_capability=Capability.OBSERVE, clock=Clock())
    obs = b.observe()
    assert len(obs.sensors) + obs.dropped == N_DEFAULT_SENSORS
    with pytest.raises(CapabilityError):
        propose(b)
    assert a.steps == 0


def test_even_a_control_bridge_cannot_change_the_site():
    a = FakeEnergySiteAdapter()
    clock = Clock()
    b = Bridge(a, Envelope(cfg()), org_id="org_demo", max_capability=Capability.CONTROL, clock=clock)
    with pytest.raises((PhysicalError, ValueError)):  # fail closed at whichever step first refuses
        p = propose(b)
        b.approve(p, "person:alice")
        b.apply(p)
    assert a.t == 0.0 and a.steps == 0 and not hasattr(a, "applied")


# ---- configuration checks
def test_bad_configuration_is_refused():
    for bad_seed in (True, "1", 1.5, None):
        rejects(seed=bad_seed)
    for bad in (0.0, -1.0, float("nan"), float("inf"), True, "60"):
        rejects(step_s=bad)
    for bad in (-0.1, 0.5, 1.0, float("nan"), True):
        rejects(noise=bad)
    for start, end in ((6.0, 6.0), (22.0, 6.0), (-1.0, 5.0), (0.0, 25.0), (float("nan"), 5.0), (0.0, float("inf"))):
        rejects(shift_start_h=start, shift_end_h=end)


def test_bad_meters_are_refused():
    rejects(meters=[])
    rejects(meters=[MeterSpec(f"m{i}", 1.0, 1.0) for i in range(MAX_METERS + 1)])
    rejects(meters=[MeterSpec("a", 1.0, 1.0), MeterSpec("a", 2.0, 2.0)])
    rejects(meters=[MeterSpec("bad id", 1.0, 1.0)])
    rejects(meters=[MeterSpec("x" * 51, 1.0, 1.0)])
    rejects(meters=[MeterSpec("a", -1.0, 1.0)])
    rejects(meters=[MeterSpec("a", 1.0, -1.0)])
    rejects(meters=[MeterSpec("a", float("nan"), 1.0)])
    rejects(meters=[MeterSpec("a", 1.0, float("inf"))])
    rejects(meters=[MeterSpec("a", True, 1.0)])
    rejects(meters=[("a", 1.0, 1.0)])
    FakeEnergySiteAdapter(meters=[MeterSpec("a", 0.0, 0.0)])  # zero is fine


# ---- invalid advance input and non-finite numbers (cases found in review)
def test_advance_refuses_a_bad_time_target_and_changes_nothing():
    a = FakeEnergySiteAdapter()
    for bad in (None, "100", True, [1.0], float("nan"), float("inf"), float("-inf"), -1.0, -1):
        with pytest.raises(ValueError):
            a.advance(bad, 10)  # an infinite target would otherwise run until max_steps
    assert a.t == 0.0 and a.steps == 0
    assert a.advance(3600, 100) is True and a.steps == 60  # a whole number of seconds is fine


def test_advance_refuses_a_bad_step_limit_and_changes_nothing():
    a = FakeEnergySiteAdapter()
    for bad in (None, "10", 2.5, True, False, -1, float("inf"), float("nan"), [1]):
        with pytest.raises(ValueError):
            a.advance(60.0, bad)
    assert a.t == 0.0 and a.steps == 0
    assert a.advance(60.0, 0) is False and a.steps == 0  # zero steps is allowed and does nothing


def test_the_power_and_step_limits_are_enforced():
    for kw in (dict(step_s=MAX_STEP_S * 2), dict(step_s=1.0e308),
               dict(meters=[MeterSpec("a", MAX_POWER_W * 10, 1.0)]), dict(meters=[MeterSpec("a", 1.0, MAX_POWER_W * 10)]),
               dict(meters=[MeterSpec("a", 1.0e308, 1.0)]), dict(meters=[MeterSpec("a", 1.0, 1.0e308)])):
        rejects(**kw)
    FakeEnergySiteAdapter(step_s=MAX_STEP_S, meters=[MeterSpec("a", MAX_POWER_W, MAX_POWER_W)])  # exactly at the limits is fine
    assert MAX_POWER_W == 1.0e12 and MAX_STEP_S == 86400.0  # the limits are part of the contract


def test_the_largest_allowed_settings_never_produce_a_non_finite_number():
    a = FakeEnergySiteAdapter(seed=1, noise=0.49, step_s=MAX_STEP_S,
                              meters=[MeterSpec(f"m{i}", MAX_POWER_W, MAX_POWER_W) for i in range(MAX_METERS)])
    assert a.advance(MAX_STEP_S * 2000, 2000) is True
    raw = a.observe_raw()
    assert math.isfinite(raw["sim_time_s"]) and all(math.isfinite(s["value"]) for s in raw["sensors"])
    obs = Observation.from_raw(raw, org_id="org_demo", observation_id="obs_000001")
    assert len(obs.sensors) + obs.dropped == len(raw["sensors"]) and obs.sensors[0].id == "shift_active"
