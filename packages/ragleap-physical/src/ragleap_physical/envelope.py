"""The safety envelope. Limits live here, in the bridge, in one place.

Default deny. Violations are rejected, never silently clamped. The envelope is built once
from an operator-supplied mapping and has no setter: no tool or model output can change it.
Supported in v0: allowed_commands, targets, rate, episode, workspace. Any other key is refused.
The workspace is a box (x, y, z in metres) that every tracked point must stay inside, before, during and after.
If a workspace is set and the adapter cannot report positions, everything is refused (fail closed).
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping, Optional

from .errors import ConfigError, EnvelopeViolation
from .observation import Observation, finite, ident
from .proposal import Proposal

SUPPORTED_COMMANDS = frozenset({"set_position_target", "set_tool_offset"})
_KEYS = {"envelope_version", "allowed_commands", "targets", "rate", "episode", "on_violation", "workspace"}
_TARGET_KEYS = {"pos_min", "pos_max", "vel_max", "force_max"}
_OFFSET_KEYS = {"offset_min", "offset_max", "step_max", "min_gap_s"}


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
            if not isinstance(lim, dict) or set(lim) not in (_TARGET_KEYS, _OFFSET_KEYS):
                raise ConfigError(f"target {name!r} needs exactly {sorted(_TARGET_KEYS)} (a joint) or {sorted(_OFFSET_KEYS)} (a tool offset)")
            vals = {k: _num(v, f"{name}.{k}") for k, v in lim.items()}
            if set(lim) == _TARGET_KEYS:
                if vals["pos_min"] >= vals["pos_max"] or vals["vel_max"] <= 0 or vals["force_max"] <= 0:
                    raise ConfigError(f"target {name!r} has impossible limits")
            elif vals["offset_min"] >= vals["offset_max"] or vals["step_max"] <= 0 or vals["min_gap_s"] < 0:
                raise ConfigError(f"target {name!r} has impossible limits")
            targets[name] = vals
        rate, episode = cfg.get("rate"), cfg.get("episode")
        if not isinstance(rate, dict) or set(rate) != {"max_commands_per_s"} or not _is_pos_int(rate["max_commands_per_s"]):
            raise ConfigError("rate needs max_commands_per_s (positive integer)")
        if (not isinstance(episode, dict) or set(episode) != {"max_sim_time_s", "max_steps"}
                or _num(episode["max_sim_time_s"], "max_sim_time_s") <= 0 or not _is_pos_int(episode["max_steps"])):
            raise ConfigError("episode needs max_sim_time_s (>0) and max_steps (positive integer)")
        ws = cfg.get("workspace")
        if ws is not None:
            if not isinstance(ws, dict) or set(ws) != {"x", "y", "z"}:
                raise ConfigError("workspace needs exactly x, y and z")
            for axis in ("x", "y", "z"):
                rng = ws[axis]
                if not isinstance(rng, (list, tuple)) or len(rng) != 2:
                    raise ConfigError("workspace." + axis + " needs [low, high]")
                lo, hi = _num(rng[0], "workspace." + axis), _num(rng[1], "workspace." + axis)
                if lo >= hi:
                    raise ConfigError("workspace." + axis + " has low >= high")
                ws[axis] = [lo, hi]
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

    def check_static(self, p: Proposal, obs: Optional[Observation] = None) -> None:
        """Checks on the proposal itself (command, target, unit, range, outcome window) and, when the current
        observation is given, on the step size and the minimum gap for tool offsets."""
        if p.command not in self._cfg["allowed_commands"]:
            raise EnvelopeViolation("command_not_allowed", "command is not on the allowed list")
        lim = self._cfg["targets"].get(p.target)
        if lim is None:
            raise EnvelopeViolation("target_not_allowed", "target is not listed")
        is_offset = "offset_min" in lim
        if p.command == "set_position_target":
            if is_offset:
                raise EnvelopeViolation("target_kind", "this target is a tool offset, not a joint")
            if p.value_unit != "rad":
                raise EnvelopeViolation("unit", "set_position_target needs unit rad")
            if not lim["pos_min"] <= p.value <= lim["pos_max"]:
                raise EnvelopeViolation("range", "value is outside the position range")
        else:  # set_tool_offset
            if not is_offset:
                raise EnvelopeViolation("target_kind", "this target is a joint, not a tool offset")
            if p.value_unit != "um":
                raise EnvelopeViolation("unit", "set_tool_offset needs unit um")
            if not lim["offset_min"] <= p.value <= lim["offset_max"]:
                raise EnvelopeViolation("range", "value is outside the offset range")
            self._check_offset_state(p, lim, obs)
        o = p.expected_outcome
        if o.entity != p.target:
            raise EnvelopeViolation("outcome_entity", "expected outcome must be about the target")
        if o.by_sim_time_s > self._cfg["episode"]["max_sim_time_s"]:
            raise EnvelopeViolation("episode", "expected outcome is beyond the episode time limit")

    @staticmethod
    def _check_offset_state(p: Proposal, lim: dict, obs: Optional[Observation]) -> None:
        """Step size and minimum gap need the current offset and the time since the last change. Unknown: refuse."""
        if obs is None:
            return
        current = next((e.pos for e in obs.entities if e.id == p.target and e.pos_unit == "um"), None)
        since = next((s.value for s in obs.sensors if s.id == "since_change_s" and s.unit == "s"), None)
        if current is None or since is None:
            raise EnvelopeViolation("state_unknown", "the current offset or the time since the last change is not reported")
        if abs(p.value - current) > lim["step_max"] + 1e-9:
            raise EnvelopeViolation("step", "the change is larger than the maximum step")
        if since < lim["min_gap_s"] - 1e-9:
            raise EnvelopeViolation("min_gap", "too soon after the last change")

    def check_state(self, obs: Observation) -> None:
        """Episode caps against fresh state."""
        if obs.sim_time_s >= self._cfg["episode"]["max_sim_time_s"] or obs.step >= self.max_steps:
            raise EnvelopeViolation("episode", "episode limit reached")
        self.check_workspace(obs)

    def check_workspace(self, obs: Observation) -> None:
        """Every tracked point (sensors of kind site_position, ids NAME_x, NAME_y, NAME_z in metres) must be inside."""
        ws = self._cfg.get("workspace")
        if ws is None:
            return
        found: dict = {}
        for s in obs.sensors:
            if s.kind == "site_position" and s.unit == "m" and s.id[-2:] in ("_x", "_y", "_z"):
                found.setdefault(s.id[:-2], {})[s.id[-1]] = s.value
        points = [v for v in found.values() if set(v) == {"x", "y", "z"}]
        if not points:
            raise EnvelopeViolation("workspace_unknown", "no position is reported, so the workspace cannot be checked")
        for point in points:
            for axis, (lo, hi) in ws.items():
                if not lo <= point[axis] <= hi:
                    raise EnvelopeViolation("workspace", "a tracked point is outside the workspace")

    def check_dry_run(self, target: str, pred) -> None:
        """Dry-run result: speed and force of every joint, and the workspace, before anything moves."""
        limits = self._cfg["targets"]
        if getattr(pred, "per_joint", None) is not None:
            for name, (vel, force) in pred.per_joint.items():
                lim = limits.get(name)
                if lim is None:
                    if vel > 1e-9 or force > 1e-9:
                        raise EnvelopeViolation("joint_not_allowed", "a joint that is not in the envelope would move")
                    continue
                self._check_vf(lim, vel, force)
        elif "vel_max" in limits[target]:
            self._check_vf(limits[target], pred.max_abs_vel, pred.max_abs_force)
        ws = self._cfg.get("workspace")
        if ws is not None:
            lo_seen, hi_seen = getattr(pred, "site_min", None), getattr(pred, "site_max", None)
            if lo_seen is None or hi_seen is None:
                raise EnvelopeViolation("workspace_unknown", "the dry run reports no positions, so the workspace cannot be checked")
            for i, axis in enumerate(("x", "y", "z")):
                if lo_seen[i] < ws[axis][0] or hi_seen[i] > ws[axis][1]:
                    raise EnvelopeViolation("workspace", "the predicted motion leaves the workspace")

    @staticmethod
    def _check_vf(lim: dict, vel: float, force: float) -> None:
        if vel > lim["vel_max"]:
            raise EnvelopeViolation("vel", "predicted speed is above the limit")
        if force > lim["force_max"]:
            raise EnvelopeViolation("force", "predicted force is above the limit")

    def check_prediction(self, target: str, max_abs_vel: float, max_abs_force: float) -> None:
        """Dry-run result against speed and force limits."""
        lim = self._cfg["targets"][target]
        if max_abs_vel > lim["vel_max"]:
            raise EnvelopeViolation("vel", "predicted speed is above the limit")
        if max_abs_force > lim["force_max"]:
            raise EnvelopeViolation("force", "predicted force is above the limit")


def _is_pos_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0
