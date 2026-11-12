"""A tiny deterministic one-joint simulator, standard library only. Used for tests and examples.

It is NOT a physics engine. It shows the bridge works without any heavy dependency.
"""
from __future__ import annotations

from typing import Any, Mapping
import hashlib
import json
import math

from .base import Adapter, DryRun

_SHA = "0" * 64


def _clamp(x: float, lim: float) -> float:
    return max(-lim, min(lim, x))


def _finite_value(value) -> float:
    """A command value must be a real, finite number: not a boolean, text, None, NaN or infinity."""
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("value must be a finite number")
    return float(value)


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

    def targets(self):
        return ["joint_1"]

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
        if target != "joint_1" or command != "set_position_target":
            raise ValueError("unsupported target or command")
        value = _finite_value(value)
        pos, vel, step = self.pos, self.vel, self.step
        max_vel = max_force = 0.0
        n = 0
        while step * self.dt < until_sim_time_s - 1e-9 and n < max_steps:
            pos, vel, step, acc = self._tick(pos, vel, step, value)
            max_vel, max_force, n = max(max_vel, abs(vel)), max(max_force, abs(acc) * self.inertia), n + 1
        return DryRun(pos, max_vel, max_force, step * self.dt >= until_sim_time_s - 1e-9)

    def apply(self, target: str, command: str, value: float) -> None:
        if self.stopped:
            raise ValueError("the adapter is stopped: clear the stop first")
        if target != "joint_1" or command != "set_position_target":
            raise ValueError("unsupported target or command")
        value = _finite_value(value)
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


class FakeArmAdapter(Adapter):
    """A planar two-link arm in the x-z plane, standard library only (same geometry as the MuJoCo ``arm2_v1`` model).

    Each joint is a limited PD controller and joints do not interact. It exists so workspace limits can be tested
    without any dependency. It is NOT a physics engine.
    """

    L1, L2, BASE_Z = 0.5, 0.4, 1.0
    NAMES = ("joint_1", "joint_2")

    def __init__(self, *, seed: int = 0, pos=(0.0, 0.0), dt: float = 0.01, kp: float = 4.0, kd: float = 3.0,
                 amax: float = 4.0, inertia: float = 1.0) -> None:
        self.seed, self.dt, self.kp, self.kd, self.amax, self.inertia = seed, dt, kp, kd, amax, inertia
        self.pos, self.vel, self.target = list(pos), [0.0, 0.0], list(pos)
        self.step, self.t, self.stopped, self.applied = 0, 0.0, False, []

    @property
    def simulated(self) -> bool:
        return True

    def targets(self):
        return list(self.NAMES)

    def _site(self, pos):
        import math
        a, b = pos
        return (self.L1 * math.cos(a) + self.L2 * math.cos(a + b), 0.0,
                self.BASE_Z - self.L1 * math.sin(a) - self.L2 * math.sin(a + b))

    def observe_raw(self) -> Mapping[str, Any]:
        x, y, z = self._site(self.pos)
        return {
            "source": {"adapter": "fake", "adapter_version": "0.0.1", "engine_version": "none",
                       "model_id": "arm2_pd", "model_sha256": _SHA},
            "sim_time_s": self.t, "step": self.step, "seed": self.seed,
            "entities": [{"id": n, "kind": "joint", "pos": self.pos[i], "vel": self.vel[i],
                          "pos_unit": "rad", "vel_unit": "rad/s"} for i, n in enumerate(self.NAMES)],
            "sensors": [{"id": "ee_" + a, "kind": "site_position", "value": v, "unit": "m"}
                        for a, v in zip("xyz", (x, y, z))],
        }

    def _tick(self, pos, vel, target):
        accs = []
        for i in range(2):
            acc = _clamp(self.kp * (target[i] - pos[i]) - self.kd * vel[i], self.amax)
            vel[i] += acc * self.dt
            pos[i] += vel[i] * self.dt
            accs.append(acc)
        return accs

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        if target not in self.NAMES or command != "set_position_target":
            raise ValueError("unsupported target or command")
        value = _finite_value(value)
        idx = self.NAMES.index(target)
        pos, vel, goal, step = list(self.pos), list(self.vel), list(self.target), self.step
        goal[idx] = value
        site = self._site(pos)
        lo, hi = list(site), list(site)
        max_v, max_f = [0.0, 0.0], [0.0, 0.0]
        n = 0
        while step * self.dt < until_sim_time_s - 1e-9 and n < max_steps:
            accs = self._tick(pos, vel, goal)
            step, n = step + 1, n + 1
            site = self._site(pos)
            for k in range(3):
                lo[k], hi[k] = min(lo[k], site[k]), max(hi[k], site[k])
            for i in range(2):
                max_v[i], max_f[i] = max(max_v[i], abs(vel[i])), max(max_f[i], abs(accs[i]) * self.inertia)
        return DryRun(pos[idx], max(max_v), max(max_f), step * self.dt >= until_sim_time_s - 1e-9,
                      per_joint={n_: (max_v[i], max_f[i]) for i, n_ in enumerate(self.NAMES)},
                      site_min=tuple(lo), site_max=tuple(hi))

    def apply(self, target: str, command: str, value: float) -> None:
        if self.stopped:
            raise ValueError("the adapter is stopped: clear the stop first")
        if target not in self.NAMES or command != "set_position_target":
            raise ValueError("unsupported target or command")
        value = _finite_value(value)
        self.target[self.NAMES.index(target)] = value
        self.applied.append((target, command, value))

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        n = 0
        while self.step * self.dt < to_sim_time_s - 1e-9 and n < max_steps and not self.stopped:
            self._tick(self.pos, self.vel, self.target)
            self.step, n = self.step + 1, n + 1
            self.t = self.step * self.dt
        return self.step * self.dt >= to_sim_time_s - 1e-9

    def stop(self) -> None:
        self.stopped, self.target, self.vel = True, list(self.pos), [0.0, 0.0]

    def clear_stop(self) -> None:
        self.stopped = False

class FakeArm3Adapter(FakeArmAdapter):
    """Three-hinge planar stand-in; deterministic PD, not a physics engine.

    Workspace coordinates describe the end-effector only. Limits belong to Envelope.
    """

    LENGTHS = (0.4, 0.3, 0.2)
    NAMES = ("joint_1", "joint_2", "joint_3")

    def __init__(self, *, seed: int = 0, pos=(0.0, 0.0, 0.0), dt: float = 0.01,
                 kp: float = 4.0, kd: float = 3.0, amax: float = 4.0,
                 inertia: float = 1.0) -> None:
        try:
            values = tuple(pos)
        except (TypeError, ValueError):
            raise ValueError("three finite joint positions required") from None
        if len(values) != 3:
            raise ValueError("three joint positions required")
        numeric_pos = tuple(self._finite_number(v, "position") for v in values)
        dt = self._finite_number(dt, "dt")
        kp = self._finite_number(kp, "kp")
        kd = self._finite_number(kd, "kd")
        amax = self._finite_number(amax, "amax")
        inertia = self._finite_number(inertia, "inertia")
        if dt <= 0 or kp < 0 or kd < 0 or amax <= 0 or inertia <= 0:
            raise ValueError("dt, amax and inertia must be positive; gains must be non-negative")
        super().__init__(seed=seed, pos=numeric_pos, dt=dt, kp=kp, kd=kd, amax=amax, inertia=inertia)
        self.vel = [0.0, 0.0, 0.0]

    @staticmethod
    def _finite_number(value, name: str) -> float:
        try:
            valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError(f"{name} must be a finite number")
        return float(value)

    @staticmethod
    def _time_target(value, name: str) -> float:
        try:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and value >= 0)
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError(f"{name} must be a finite non-negative number")
        return float(value)

    @staticmethod
    def _step_limit(value) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("max_steps must be a non-negative integer")
        return value

    def _validated_command(self, target: str, command: str, value) -> tuple[int, float]:
        if target not in self.NAMES or command != "set_position_target":
            raise ValueError("unsupported target or command")
        numeric = self._finite_number(value, "value")
        return self.NAMES.index(target), numeric

    def _site(self, pos):
        import math
        angle = 0.0
        x, z = 0.0, self.BASE_Z
        for length, joint in zip(self.LENGTHS, pos):
            angle += joint
            x += length * math.cos(angle)
            z -= length * math.sin(angle)
        return (x, 0.0, z)

    def _tick(self, pos, vel, target):
        accs = []
        for i in range(3):
            acc = _clamp(self.kp * (target[i] - pos[i]) - self.kd * vel[i], self.amax)
            vel[i] += acc * self.dt
            pos[i] += vel[i] * self.dt
            accs.append(acc)
        return accs

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        idx, value = self._validated_command(target, command, value)
        until_sim_time_s = self._time_target(until_sim_time_s, "until_sim_time_s")
        max_steps = self._step_limit(max_steps)
        pos, vel, goal, step = list(self.pos), list(self.vel), list(self.target), self.step
        goal[idx] = value
        lo = list(self._site(pos))
        hi = list(lo)
        max_v, max_f = [0.0] * 3, [0.0] * 3
        n = 0
        while step * self.dt < until_sim_time_s - 1e-9 and n < max_steps:
            accs = self._tick(pos, vel, goal)
            step += 1
            n += 1
            site = self._site(pos)
            for k in range(3):
                lo[k], hi[k] = min(lo[k], site[k]), max(hi[k], site[k])
            for i in range(3):
                max_v[i] = max(max_v[i], abs(vel[i]))
                max_f[i] = max(max_f[i], abs(accs[i] * self.inertia))
        return DryRun(pos[idx], max(max_v), max(max_f), step * self.dt >= until_sim_time_s - 1e-9,
                      per_joint={name: (max_v[i], max_f[i]) for i, name in enumerate(self.NAMES)},
                      site_min=tuple(lo), site_max=tuple(hi))

    def apply(self, target: str, command: str, value: float) -> None:
        if self.stopped:
            raise ValueError("the adapter is stopped: clear the stop first")
        idx, numeric = self._validated_command(target, command, value)
        self.target[idx] = numeric
        self.applied.append((target, command, numeric))

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        to_sim_time_s = self._time_target(to_sim_time_s, "to_sim_time_s")
        max_steps = self._step_limit(max_steps)
        n = 0
        while self.step * self.dt < to_sim_time_s - 1e-9 and n < max_steps and not self.stopped:
            self._tick(self.pos, self.vel, self.target)
            self.step, n = self.step + 1, n + 1
            self.t = self.step * self.dt
        return self.step * self.dt >= to_sim_time_s - 1e-9

    def stop(self) -> None:
        self.stopped, self.target, self.vel = True, list(self.pos), [0.0, 0.0, 0.0]

    def observe_raw(self) -> Mapping[str, Any]:
        raw = dict(super().observe_raw())
        params = {"model_id": "arm3_pd", "lengths": self.LENGTHS, "base_z": self.BASE_Z,
                  "dt": self.dt, "kp": self.kp, "kd": self.kd, "amax": self.amax,
                  "inertia": self.inertia}
        digest = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        raw["source"] = {**raw["source"], "model_id": "arm3_pd", "model_sha256": digest}
        return raw
