"""A tiny deterministic one-joint simulator, standard library only. Used for tests and examples.

It is NOT a physics engine. It shows the bridge works without any heavy dependency.
"""
from __future__ import annotations

from typing import Any, Mapping

from .base import Adapter, DryRun

_SHA = "0" * 64


def _clamp(x: float, lim: float) -> float:
    return max(-lim, min(lim, x))


class FakeJointAdapter(Adapter):
    """One hinge joint 'joint_1' driven by a limited PD controller."""

    def __init__(self, *, seed: int = 0, pos: float = 0.0, dt: float = 0.01, kp: float = 4.0,
                 kd: float = 3.0, amax: float = 4.0, inertia: float = 1.0) -> None:
        self.seed, self.dt, self.kp, self.kd, self.amax, self.inertia = seed, dt, kp, kd, amax, inertia
        self.pos, self.vel, self.step, self.t = pos, 0.0, 0, 0.0
        self.target = pos
        self.stopped = False
        self.applied: list = []

    @property
    def simulated(self) -> bool:
        return True

    def observe_raw(self) -> Mapping[str, Any]:
        return {
            "source": {"adapter": "fake", "adapter_version": "0.0.1", "engine_version": "none",
                       "model_id": "joint_1_pd", "model_sha256": _SHA},
            "sim_time_s": self.t, "step": self.step, "seed": self.seed,
            "entities": [{"id": "joint_1", "kind": "joint", "pos": self.pos, "vel": self.vel,
                          "pos_unit": "rad", "vel_unit": "rad/s"}],
            "sensors": [],
        }

    def _tick(self, pos: float, vel: float, step: int, target: float):
        acc = _clamp(self.kp * (target - pos) - self.kd * vel, self.amax)
        vel += acc * self.dt
        pos += vel * self.dt
        return pos, vel, step + 1, acc

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        pos, vel, step = self.pos, self.vel, self.step
        max_vel = max_force = 0.0
        n = 0
        while step * self.dt < until_sim_time_s - 1e-9 and n < max_steps:
            pos, vel, step, acc = self._tick(pos, vel, step, value)
            max_vel, max_force, n = max(max_vel, abs(vel)), max(max_force, abs(acc) * self.inertia), n + 1
        return DryRun(pos, max_vel, max_force, step * self.dt >= until_sim_time_s - 1e-9)

    def apply(self, target: str, command: str, value: float) -> None:
        if target != "joint_1" or command != "set_position_target":
            raise ValueError("unsupported target or command")
        self.target = value
        self.applied.append((target, command, value))

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        n = 0
        while self.step * self.dt < to_sim_time_s - 1e-9 and n < max_steps and not self.stopped:
            self.pos, self.vel, self.step, _ = self._tick(self.pos, self.vel, self.step, self.target)
            self.t, n = self.step * self.dt, n + 1
        return self.step * self.dt >= to_sim_time_s - 1e-9

    def stop(self) -> None:
        self.stopped, self.target, self.vel = True, self.pos, 0.0

    def clear_stop(self) -> None:
        self.stopped = False
