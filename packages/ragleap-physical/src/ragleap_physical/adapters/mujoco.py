"""MuJoCo adapters. Needs the optional extra: pip install "ragleap-physical[mujoco]".

``MujocoAdapter`` is observe-only: dry_run and apply refuse. ``MujocoServoAdapter`` can follow
``set_position_target`` through a position actuator, but only behind the bridge (limits are NOT enforced
here). Only hinge and slide joints are reported; other joint types are skipped for now.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Mapping, Optional

from .base import Adapter, DryRun

PENDULUM_V1_XML = """<mujoco model="pendulum_v1">
  <option timestep="0.002"/>
  <worldbody>
    <body pos="0 0 1">
      <joint name="joint_1" type="hinge" axis="0 1 0" damping="0.05"/>
      <geom type="capsule" size="0.05" fromto="0 0 0 0.5 0 0" mass="1"/>
    </body>
  </worldbody>
</mujoco>"""


class MujocoAdapter(Adapter):
    """Observe-only adapter. The model file is treated as untrusted input by the caller."""

    def __init__(self, model_xml: Optional[str] = None, *, model_id: str = "pendulum_v1", seed: int = 0,
                 initial: Optional[Mapping[str, float]] = None) -> None:
        try:
            import mujoco
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ImportError('MuJoCo is not installed. Use: pip install "ragleap-physical[mujoco]"') from exc
        self._mj = mujoco
        if model_xml is None:
            model_xml, initial = PENDULUM_V1_XML, {"joint_1": 0.3, **dict(initial or {})}
        self._model_id, self._seed = model_id, int(seed)
        self._sha = hashlib.sha256(model_xml.encode("utf-8")).hexdigest()
        self._m = mujoco.MjModel.from_xml_string(model_xml)
        self._d = mujoco.MjData(self._m)
        for name, value in (initial or {}).items():
            jid = mujoco.mj_name2id(self._m, mujoco.mjtObj.mjOBJ_JOINT, name)
            if jid < 0:
                raise ValueError("unknown joint in initial state")
            self._d.qpos[self._m.jnt_qposadr[jid]] = float(value)
        mujoco.mj_forward(self._m, self._d)
        self._step, self._stopped = 0, False

    @property
    def simulated(self) -> bool:
        return True

    def observe_raw(self) -> Mapping[str, Any]:
        mj, m, d = self._mj, self._m, self._d
        entities = []
        for j in range(m.njnt):
            kind = m.jnt_type[j]
            if kind == mj.mjtJoint.mjJNT_HINGE:
                pu, vu = "rad", "rad/s"
            elif kind == mj.mjtJoint.mjJNT_SLIDE:
                pu, vu = "m", "m/s"
            else:
                continue
            name = mj.mj_id2name(m, mj.mjtObj.mjOBJ_JOINT, j) or f"joint_{j}"
            entities.append({"id": name, "kind": "joint", "pos": float(d.qpos[m.jnt_qposadr[j]]),
                             "vel": float(d.qvel[m.jnt_dofadr[j]]), "pos_unit": pu, "vel_unit": vu})
        mj.mj_kinematics(m, d)  # make body and site positions match the current joint state
        sensors = []
        for sid in range(m.nsite):
            name = mj.mj_id2name(m, mj.mjtObj.mjOBJ_SITE, sid) or f"site_{sid}"
            for axis, value in zip("xyz", d.site_xpos[sid]):
                sensors.append({"id": f"{name}_{axis}", "kind": "site_position", "value": float(value), "unit": "m"})
        return {"source": {"adapter": "mujoco", "adapter_version": "0.0.1", "engine_version": mj.__version__,
                           "model_id": self._model_id, "model_sha256": self._sha},
                "sim_time_s": float(d.time), "step": self._step, "seed": self._seed,
                "entities": entities, "sensors": sensors}

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        raise NotImplementedError("control is not implemented for MuJoCo yet (Phase 3)")

    def apply(self, target: str, command: str, value: float) -> None:
        raise NotImplementedError("control is not implemented for MuJoCo yet (Phase 3)")

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        n = 0
        while self._d.time < to_sim_time_s - 1e-12 and n < max_steps and not self._stopped:
            self._mj.mj_step(self._m, self._d)
            self._step, n = self._step + 1, n + 1
        return self._d.time >= to_sim_time_s - 1e-12

    def stop(self) -> None:
        self._stopped = True

    def clear_stop(self) -> None:
        self._stopped = False


SERVO_V1_XML = """<mujoco model="servo_v1">
  <option timestep="0.002"/>
  <worldbody>
    <body pos="0 0 1" gravcomp="1">
      <joint name="joint_1" type="hinge" axis="0 1 0" damping="0.05"/>
      <geom type="capsule" size="0.05" fromto="0 0 0 0.5 0 0" mass="1"/>
    </body>
  </worldbody>
  <actuator>
    <position name="act_joint_1" joint="joint_1" kp="10" kv="1" ctrllimited="true" ctrlrange="-3.1 3.1"/>
  </actuator>
</mujoco>"""


ARM2_XML = """<mujoco model="arm2_v1">
  <option timestep="0.002"/>
  <worldbody>
    <body name="link1" pos="0 0 1" gravcomp="1">
      <joint name="joint_1" type="hinge" axis="0 1 0" damping="0.05"/>
      <geom type="capsule" size="0.04" fromto="0 0 0 0.5 0 0" mass="1"/>
      <body name="link2" pos="0.5 0 0" gravcomp="1">
        <joint name="joint_2" type="hinge" axis="0 1 0" damping="0.05"/>
        <geom type="capsule" size="0.03" fromto="0 0 0 0.4 0 0" mass="0.6"/>
        <site name="ee" pos="0.4 0 0" size="0.02"/>
      </body>
    </body>
  </worldbody>
  <actuator>
    <position name="act_joint_1" joint="joint_1" kp="10" kv="4" ctrllimited="true" ctrlrange="-3.1 3.1"/>
    <position name="act_joint_2" joint="joint_2" kp="6" kv="1.2" ctrllimited="true" ctrlrange="-3.1 3.1"/>
  </actuator>
</mujoco>"""


class MujocoServoAdapter(MujocoAdapter):
    """Follows ``set_position_target`` on joints that have a position actuator. Simulation only.

    The bridge enforces every limit before this class is called. This class does no clamping.
    """

    def __init__(self, model_xml: Optional[str] = None, *, model_id: str = "servo_v1", seed: int = 0,
                 initial: Optional[Mapping[str, float]] = None) -> None:
        if model_xml is None:
            model_xml, initial = SERVO_V1_XML, {"joint_1": 0.0, **dict(initial or {})}
        super().__init__(model_xml, model_id=model_id, seed=seed, initial=initial)
        mj, m = self._mj, self._m
        self._act: dict = {}
        for a in range(m.nu):
            if m.actuator_trntype[a] == mj.mjtTrn.mjTRN_JOINT:
                jid = int(m.actuator_trnid[a, 0])
                name = mj.mj_id2name(m, mj.mjtObj.mjOBJ_JOINT, jid)
                if name:
                    self._act[name] = (a, jid)
        if not self._act:
            raise ValueError("the model has no joint actuator")
        self._hold()

    def _hold(self) -> None:
        """Point every actuator at the joint's current position."""
        for a, jid in self._act.values():
            self._d.ctrl[a] = self._d.qpos[self._m.jnt_qposadr[jid]]

    def _need(self, target: str, command: str, value):
        if command != "set_position_target" or target not in self._act:
            raise ValueError("unsupported target or command")
        try:
            valid_number = (not isinstance(value, bool) and isinstance(value, (int, float))
                            and math.isfinite(value))
        except (OverflowError, TypeError, ValueError):
            valid_number = False
        if not valid_number:
            raise ValueError("value must be a finite number")
        act, jid = self._act[target]
        if self._m.actuator_ctrllimited[act]:
            lo, hi = self._m.actuator_ctrlrange[act]
            if value < lo or value > hi:
                raise ValueError(f"value outside actuator ctrlrange [{lo}, {hi}]")
        return act, jid, float(value)

    def targets(self) -> list:
        return list(self._act)

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        a, jid, value = self._need(target, command, value)
        mj, m = self._mj, self._m
        copy = mj.MjData(m)
        mj.mj_copyData(copy, m, self._d)  # the real state is never touched
        copy.ctrl[a] = value
        per = {name: [0.0, 0.0] for name in self._act}
        box = {"lo": None, "hi": None}

        def track() -> None:
            mj.mj_kinematics(m, copy)
            for sid in range(m.nsite):
                p = [float(v) for v in copy.site_xpos[sid]]
                box["lo"] = p if box["lo"] is None else [min(x, y) for x, y in zip(box["lo"], p)]
                box["hi"] = p if box["hi"] is None else [max(x, y) for x, y in zip(box["hi"], p)]

        track()
        n = 0
        while copy.time < until_sim_time_s - 1e-12 and n < max_steps:
            mj.mj_step(m, copy)
            track()
            for name, (act, j) in self._act.items():
                per[name][0] = max(per[name][0], abs(float(copy.qvel[m.jnt_dofadr[j]])))
                per[name][1] = max(per[name][1], abs(float(copy.actuator_force[act])))
            n += 1
        return DryRun(float(copy.qpos[m.jnt_qposadr[jid]]), max(v for v, _ in per.values()),
                      max(f for _, f in per.values()), copy.time >= until_sim_time_s - 1e-12,
                      per_joint={k: (v[0], v[1]) for k, v in per.items()},
                      site_min=tuple(box["lo"]) if box["lo"] else None, site_max=tuple(box["hi"]) if box["hi"] else None)

    def apply(self, target: str, command: str, value: float) -> None:
        if self._stopped:
            raise ValueError("the adapter is stopped: clear the stop first")
        a, _, value = self._need(target, command, value)
        self._d.ctrl[a] = value

    def stop(self) -> None:
        super().stop()
        self._hold()

