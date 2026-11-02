"""A simulated site with a few electricity meters, standard library only. OBSERVE ONLY: it has no command at all.

Each meter has a power draw while the shift is running and a (usually lower) draw outside it. Some of the draw
outside the shift is waste (a compressor leak, lights left on). The adapter only reports sensors; it never says
which load is waste.

It is a MODEL with invented numbers, not a physics engine and not a real site. It reads no real meter.

Time: ``advance`` moves simulated time in steps of ``step_s`` seconds. A day is 24 simulated hours and starts at
00:00. Energy is the sum of power x step (rectangle rule, end-of-step power), in kWh.
Limits that keep every number finite: power per meter at most MAX_POWER_W, one step at most MAX_STEP_S.
Units used (W and kWh are approved by A; until observation.py lists them the bridge drops those sensors and
counts them in ``dropped``): W, kWh, 1.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from ..errors import ValidationError
from ..observation import ident
from .base import Adapter, DryRun

MAX_METERS = 50  # two sensors per meter plus a few site sensors must stay under the observation cap of 256
MAX_ID_LEN = 50  # "energy_" + id must still be a safe identifier of at most 64 characters
DAY_S = 86400.0
MAX_POWER_W = 1.0e12  # one terawatt: far above any site; keeps every power and energy number finite
MAX_STEP_S = 86400.0  # one step is at most one simulated day
J_PER_KWH = 3_600_000.0


@dataclass(frozen=True)
class MeterSpec:
    """One meter. ``run_w``: mean power during the shift. ``idle_w``: mean power outside it. Both in watts."""

    id: str
    run_w: float
    idle_w: float


DEFAULT_METERS = (
    MeterSpec("line_1", 18000.0, 400.0),
    MeterSpec("compressor", 7500.0, 1800.0),  # idle draw is a leak: the "waste" in this site
    MeterSpec("hvac", 5000.0, 2500.0),
    MeterSpec("lighting", 1200.0, 900.0),  # left on at night
)


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number")
    return float(value)


class FakeEnergySiteAdapter(Adapter):
    """Observation: no entities, sensors only (power and cumulative energy per meter, site totals, shift flag)."""

    def __init__(self, *, seed: int = 0, meters: Sequence[MeterSpec] = DEFAULT_METERS, step_s: float = 60.0,
                 noise: float = 0.02, shift_start_h: float = 6.0, shift_end_h: float = 22.0) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        step_s, noise = _number(step_s, "step_s"), _number(noise, "noise")
        start, end = _number(shift_start_h, "shift_start_h"), _number(shift_end_h, "shift_end_h")
        if step_s <= 0:
            raise ValueError("step_s must be positive")
        if step_s > MAX_STEP_S:
            raise ValidationError(f"step_s must not be above {MAX_STEP_S:g} seconds")
        if not 0.0 <= noise < 0.5:
            raise ValueError("noise must be at least 0 and below 0.5")
        if not 0.0 <= start < end <= 24.0:
            raise ValueError("the shift must satisfy 0 <= start < end <= 24 hours")
        meters = tuple(meters)
        if not 1 <= len(meters) <= MAX_METERS:
            raise ValueError(f"there must be between 1 and {MAX_METERS} meters")
        seen = set()
        for m in meters:
            if not isinstance(m, MeterSpec):
                raise ValueError("meters must be MeterSpec objects")
            ident(m.id, "meter id")
            if len(m.id) > MAX_ID_LEN:
                raise ValueError("meter id is too long")
            if m.id in seen:
                raise ValueError("meter ids must be unique")
            seen.add(m.id)
            if _number(m.run_w, "run_w") < 0 or _number(m.idle_w, "idle_w") < 0:
                raise ValueError("power must not be negative")
            if m.run_w > MAX_POWER_W or m.idle_w > MAX_POWER_W:
                raise ValidationError(f"power must not be above {MAX_POWER_W:g} W")
        self.seed, self.meters, self.step_s, self.noise = seed, meters, step_s, noise
        self.shift_start_h, self.shift_end_h = start, end
        self._rng = random.Random(seed)
        self.t, self.steps, self.stopped = 0.0, 0, False
        self._shift = self._in_shift(0.0)
        self._power = {m.id: self._mean(m, self._shift) for m in meters}  # noise-free at t = 0
        self._energy_kwh = {m.id: 0.0 for m in meters}
        spec = [[m.id, m.run_w, m.idle_w] for m in meters] + [step_s, noise, start, end]
        self._model_sha = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()

    @property
    def simulated(self) -> bool:
        return True

    def targets(self) -> Optional[list]:
        return []  # nothing here can move

    def _in_shift(self, t: float) -> bool:
        hour = (t % DAY_S) / 3600.0
        return self.shift_start_h <= hour < self.shift_end_h

    @staticmethod
    def _mean(meter: MeterSpec, shift: bool) -> float:
        return meter.run_w if shift else meter.idle_w

    def observe_raw(self) -> Mapping[str, Any]:
        sensors = [{"id": "shift_active", "kind": "flag", "value": 1.0 if self._shift else 0.0, "unit": "1"},
                   {"id": "site_power", "kind": "power", "value": sum(self._power.values()), "unit": "W"},
                   {"id": "site_energy", "kind": "energy", "value": sum(self._energy_kwh.values()), "unit": "kWh"}]
        for m in self.meters:
            sensors.append({"id": f"power_{m.id}", "kind": "power", "value": self._power[m.id], "unit": "W"})
            sensors.append({"id": f"energy_{m.id}", "kind": "energy", "value": self._energy_kwh[m.id], "unit": "kWh"})
        return {
            "source": {"adapter": "fake", "adapter_version": "0.0.1", "engine_version": "none",
                       "model_id": "energy_site", "model_sha256": self._model_sha},
            "sim_time_s": self.t, "step": self.steps, "seed": self.seed, "entities": [], "sensors": sensors,
        }

    def _step(self) -> None:
        self.t += self.step_s
        self.steps += 1
        self._shift = self._in_shift(self.t)
        for m in self.meters:
            p = max(0.0, self._mean(m, self._shift) * (1.0 + self._rng.gauss(0.0, self.noise)))  # never negative
            self._power[m.id] = p
            self._energy_kwh[m.id] += p * self.step_s / J_PER_KWH

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        raise ValueError("this adapter is observe-only: it has no command")

    def apply(self, target: str, command: str, value: float) -> None:
        raise ValueError("this adapter is observe-only: it has no command")

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        if isinstance(to_sim_time_s, bool) or not isinstance(to_sim_time_s, (int, float)) \
                or not math.isfinite(to_sim_time_s) or to_sim_time_s < 0:
            raise ValidationError("to_sim_time_s must be a finite number, not negative")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 0:
            raise ValidationError("max_steps must be an integer, not negative")
        n = 0
        while self.t < to_sim_time_s - 1e-9 and n < max_steps and not self.stopped:
            self._step()
            n += 1
        return self.t >= to_sim_time_s - 1e-9

    def stop(self) -> None:
        self.stopped = True

    def clear_stop(self) -> None:
        self.stopped = False
