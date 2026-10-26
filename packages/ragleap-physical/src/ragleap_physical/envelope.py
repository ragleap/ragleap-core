"""The safety envelope. Limits live here, in the bridge, in one place.

Default deny. Violations are rejected, never silently clamped. The envelope is built once
from an operator-supplied mapping and has no setter: no tool or model output can change it.
Supported in v0: allowed_commands, targets, rate, episode. Any other key is refused
(workspace limits are planned, not built).
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping

from .errors import ConfigError, EnvelopeViolation
from .observation import Observation, finite, ident
from .proposal import Proposal

SUPPORTED_COMMANDS = frozenset({"set_position_target"})
_KEYS = {"envelope_version", "allowed_commands", "targets", "rate", "episode", "on_violation"}
_TARGET_KEYS = {"pos_min", "pos_max", "vel_max", "force_max"}


def _num(value: Any, what: str) -> float:
    try:
        return finite(value, what)
    except ValueError as exc:  # ValidationError is a ValueError
        raise ConfigError(str(exc)) from None


class Envelope:
    """Validated, read-only safety limits."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        cfg = copy.deepcopy(dict(config))
        unknown = set(cfg) - _KEYS
        if unknown:
            raise ConfigError(f"unknown or unsupported envelope keys: {sorted(unknown)}")
        if cfg.get("envelope_version") != 0:
            raise ConfigError("envelope_version must be 0")
        if cfg.get("on_violation") != "reject":
            raise ConfigError("on_violation must be 'reject' (clamping is not allowed)")
        cmds = cfg.get("allowed_commands")
        if not isinstance(cmds, list) or not cmds or not set(cmds) <= SUPPORTED_COMMANDS:
            raise ConfigError("allowed_commands must be a non-empty list of supported commands")
        targets = cfg.get("targets")
        if not isinstance(targets, dict) or not targets:
            raise ConfigError("targets must be a non-empty mapping")
        for name, lim in targets.items():
            try:
                ident(name, "target")
            except ValueError as exc:
                raise ConfigError(str(exc)) from None
            if not isinstance(lim, dict) or set(lim) != _TARGET_KEYS:
                raise ConfigError(f"target {name!r} needs exactly {sorted(_TARGET_KEYS)}")
            vals = {k: _num(v, f"{name}.{k}") for k, v in lim.items()}
            if vals["pos_min"] >= vals["pos_max"] or vals["vel_max"] <= 0 or vals["force_max"] <= 0:
                raise ConfigError(f"target {name!r} has impossible limits")
            targets[name] = vals
        rate, episode = cfg.get("rate"), cfg.get("episode")
        if not isinstance(rate, dict) or set(rate) != {"max_commands_per_s"} or not _is_pos_int(rate["max_commands_per_s"]):
            raise ConfigError("rate needs max_commands_per_s (positive integer)")
        if (not isinstance(episode, dict) or set(episode) != {"max_sim_time_s", "max_steps"}
                or _num(episode["max_sim_time_s"], "max_sim_time_s") <= 0 or not _is_pos_int(episode["max_steps"])):
            raise ConfigError("episode needs max_sim_time_s (>0) and max_steps (positive integer)")
        self._cfg = cfg
        self.digest = hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def max_steps(self) -> int:
        return self._cfg["episode"]["max_steps"]

    @property
    def max_commands_per_s(self) -> int:
        return self._cfg["rate"]["max_commands_per_s"]

    def as_dict(self) -> dict:
        """A copy for read-only viewing. Changing the copy changes nothing."""
        return copy.deepcopy(self._cfg)

    def check_static(self, p: Proposal) -> None:
        """Checks that need no device state: command, target, unit, range, outcome window."""
        if p.command not in self._cfg["allowed_commands"]:
            raise EnvelopeViolation("command_not_allowed", "command is not on the allowed list")
        lim = self._cfg["targets"].get(p.target)
        if lim is None:
            raise EnvelopeViolation("target_not_allowed", "target is not listed")
        if p.value_unit != "rad":
            raise EnvelopeViolation("unit", "set_position_target needs unit rad")
        if not lim["pos_min"] <= p.value <= lim["pos_max"]:
            raise EnvelopeViolation("range", "value is outside the position range")
        o = p.expected_outcome
        if o.entity != p.target:
            raise EnvelopeViolation("outcome_entity", "expected outcome must be about the target")
        if o.by_sim_time_s > self._cfg["episode"]["max_sim_time_s"]:
            raise EnvelopeViolation("episode", "expected outcome is beyond the episode time limit")

    def check_state(self, obs: Observation) -> None:
        """Episode caps against fresh state."""
        if obs.sim_time_s >= self._cfg["episode"]["max_sim_time_s"] or obs.step >= self.max_steps:
            raise EnvelopeViolation("episode", "episode limit reached")

    def check_prediction(self, target: str, max_abs_vel: float, max_abs_force: float) -> None:
        """Dry-run result against speed and force limits."""
        lim = self._cfg["targets"][target]
        if max_abs_vel > lim["vel_max"]:
            raise EnvelopeViolation("vel", "predicted speed is above the limit")
        if max_abs_force > lim["force_max"]:
            raise EnvelopeViolation("force", "predicted force is above the limit")


def _is_pos_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0
