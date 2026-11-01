"""Offline ROS 2/Gazebo-shaped observation fixture. No ROS imports or transport.

This is NOT a ROS 2 client and never connects to hardware or a network.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from .base import Adapter, DryRun


class MockROS2ObserveAdapter(Adapter):
    """Read-only simulator fixture; motion interfaces fail closed."""

    def __init__(self, *, seed: int = 0) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        self._seed = seed
        self._step = 0
        self._time = 0.0
        self._position = 0.0

    @property
    def simulated(self) -> bool:
        return True

    def targets(self) -> list:
        return []

    def ingest_fixture(self, *, position: float, sim_time_s: float, step: int) -> None:
        """Accept explicitly injected synthetic observations, never subscribe to ROS."""
        if (isinstance(position, bool) or not isinstance(position, (int, float))
                or not math.isfinite(position)):
            raise ValueError("invalid fixture position")
        if (isinstance(sim_time_s, bool) or not isinstance(sim_time_s, (int, float))
                or not math.isfinite(sim_time_s) or sim_time_s < self._time):
            raise ValueError("invalid or regressing fixture time")
        if isinstance(step, bool) or not isinstance(step, int) or step < self._step:
            raise ValueError("invalid or regressing fixture step")
        self._position = float(position)
        self._time = float(sim_time_s)
        self._step = step

    def observe_raw(self) -> Mapping[str, Any]:
        return {
            "source": {"adapter": "ros2_mock", "adapter_version": "0.0.1",
                       "engine_version": "none", "model_id": "offline_joint_fixture",
                       "model_sha256": "0" * 64},
            "sim_time_s": self._time, "step": self._step, "seed": self._seed,
            "entities": [{"id": "joint_1", "kind": "joint", "pos": self._position,
                          "vel": 0.0, "pos_unit": "rad", "vel_unit": "rad/s"}],
            "sensors": [],
        }

    def dry_run(self, target: str, command: str, value: float,
                until_sim_time_s: float, max_steps: int) -> DryRun:
        raise PermissionError("observe-only adapter: dry_run disabled")

    def apply(self, target: str, command: str, value: float) -> None:
        raise PermissionError("observe-only adapter: apply disabled")

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        raise PermissionError("observe-only adapter: advance disabled")

    def stop(self) -> None:
        # No actuators or transport exist; stop has nothing to command.
        return None

    def clear_stop(self) -> None:
        raise PermissionError("observe-only adapter: clear_stop disabled")
