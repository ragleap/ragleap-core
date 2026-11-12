"""A simulated machine tool, standard library only: a cutting tool wears part by part.

The only thing the bridge can change is the tool offset (``set_tool_offset``, micrometres). Changing the tool itself
is a human action: ``replace_tool()`` exists for the operator side and is NOT reachable through any command.
It is a MODEL with invented numbers, not a physics engine and not a real machine.

Time: one part takes one second of simulated time. Part error = wear - offset + scatter. Every ``measure_every``
parts the gauge reads the error with noise. A part is scrap if |error| > ``tol``.
"""
from __future__ import annotations

import math
import random
from typing import Any, Mapping

from .base import Adapter, DryRun

_SHA = "0" * 64


def _finite_value(value) -> float:
    """A command value must be a real, finite number: not a boolean, text, None, NaN or infinity."""
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("value must be a finite number")
    return float(value)


class FakeMachineToolAdapter(Adapter):
    """One tool, ``tool_1``. Observation: the offset as an entity (um) and a few sensors."""

    TARGET = "tool_1"

    def __init__(self, *, seed: int = 0, meas_sigma: float = 1.0, proc_sigma: float = 1.5, measure_every: int = 5,
                 base: float = 0.35, accel: float = 0.00035, burst_prob: float = 0.004, tol: float = 10.0) -> None:
        self._rng = random.Random(seed)
        self.seed, self.meas_sigma, self.proc_sigma, self.measure_every = seed, meas_sigma, proc_sigma, measure_every
        self.base, self.accel, self.burst_prob, self.tol = base, accel, burst_prob, tol
        self.parts, self.t, self.wear, self.offset, self.tool_age, self.scrap = 0, 0.0, 0.0, 0.0, 0, 0
        self.last_measurement, self.measurement_age, self.since_change = 0.0, 0, 1.0e6
        self.stopped, self.applied = False, []

    @property
    def simulated(self) -> bool:
        return True

    def targets(self):
        return [self.TARGET]

    def observe_raw(self) -> Mapping[str, Any]:
        return {
            "source": {"adapter": "fake", "adapter_version": "0.0.1", "engine_version": "none",
                       "model_id": "machine_tool_wear", "model_sha256": _SHA},
            "sim_time_s": self.t, "step": self.parts, "seed": self.seed,
            "entities": [{"id": self.TARGET, "kind": "offset", "pos": self.offset, "vel": 0.0,
                          "pos_unit": "um", "vel_unit": "1"}],
            "sensors": [
                {"id": "measured_error", "kind": "measurement", "value": self.last_measurement, "unit": "um"},
                {"id": "measurement_age", "kind": "count", "value": float(self.measurement_age), "unit": "1"},
                {"id": "tool_age", "kind": "count", "value": float(self.tool_age), "unit": "1"},
                {"id": "scrap_count", "kind": "count", "value": float(self.scrap), "unit": "1"},
                {"id": "since_change_s", "kind": "timer", "value": self.since_change, "unit": "s"},
            ],
        }

    def _make_part(self) -> None:
        rate = self.base * (1 + self.accel * self.wear ** 2) * self._rng.lognormvariate(0, 0.25)
        if self._rng.random() < self.burst_prob:
            rate += self._rng.uniform(3, 8)  # a chip or a hard spot
        self.wear += rate
        err = self.wear - self.offset + self._rng.gauss(0, self.proc_sigma)
        self.parts += 1
        self.t = float(self.parts)
        self.tool_age += 1
        self.since_change += 1.0
        self.measurement_age += 1
        if abs(err) > self.tol:
            self.scrap += 1
        if self.parts % self.measure_every == 0:
            self.last_measurement = err + self._rng.gauss(0, self.meas_sigma)
            self.measurement_age = 0

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        if target != self.TARGET or command != "set_tool_offset":
            raise ValueError("unsupported target or command")
        value = _finite_value(value)
        return DryRun(value, 0.0, 0.0, True, per_joint={})

    def apply(self, target: str, command: str, value: float) -> None:
        if self.stopped:
            raise ValueError("the adapter is stopped: clear the stop first")
        if target != self.TARGET or command != "set_tool_offset":
            raise ValueError("unsupported target or command")
        value = _finite_value(value)  # checked before anything changes: a refused value leaves the adapter untouched
        self.offset, self.since_change = value, 0.0
        self.applied.append((target, command, value))

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        n = 0
        while self.t < to_sim_time_s - 1e-9 and n < max_steps and not self.stopped:
            self._make_part()
            n += 1
        return self.t >= to_sim_time_s - 1e-9

    def stop(self) -> None:
        self.stopped = True

    def clear_stop(self) -> None:
        self.stopped = False

    def replace_tool(self) -> None:
        """Operator side (a human changes the tool). Not a command: nothing in the bridge can call this."""
        self.wear, self.offset, self.tool_age, self.since_change = 0.0, 0.0, 0, 1.0e6
