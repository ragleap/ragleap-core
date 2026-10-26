"""Adapter interface. An adapter talks to one simulator. It never enforces safety: the bridge does."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class DryRun:
    """What would happen, computed on a copy. Nothing is changed."""

    final_pos: float
    max_abs_vel: float
    max_abs_force: float
    reached_time: bool


class Adapter(ABC):
    """Small abstract class every simulator adapter implements."""

    @property
    @abstractmethod
    def simulated(self) -> bool:
        """True for simulators. This version refuses anything else."""

    @abstractmethod
    def observe_raw(self) -> Mapping[str, Any]:
        """Return raw, untrusted state. The bridge sanitises it."""

    @abstractmethod
    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        """Predict the effect of a command without applying it."""

    @abstractmethod
    def apply(self, target: str, command: str, value: float) -> None:
        """Set the command. Only the bridge calls this, after approval and envelope checks."""

    @abstractmethod
    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        """Step the simulation. Return True if the time was reached within max_steps."""

    @abstractmethod
    def stop(self) -> None:
        """Go to the safe state (hold, zero velocity)."""

    @abstractmethod
    def clear_stop(self) -> None:
        """Leave the stopped state. Operator only."""
