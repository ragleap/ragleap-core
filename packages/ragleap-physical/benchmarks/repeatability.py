"""Repeatability experiment: simulation-only, exact replay within one installed environment.

Run: python benchmarks/repeatability.py --runs 3 --steps 100 --output benchmarks/result.json
Run the same command in each Python/MuJoCo environment and compare JSON reports.
No network, hardware, sleeping, or control bridge is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import struct
import sys
from pathlib import Path


def trajectory(seed: int, steps: int) -> list[tuple[float, ...]]:
    import mujoco
    xml = Path(__file__).resolve().parents[1] / 'examples' / 'robots' / 'arm3.xml'
    model = mujoco.MjModel.from_xml_string(xml.read_text(encoding='utf-8'))
    data = mujoco.MjData(model)
    # Seed is metadata: this model has no stochastic components.
    mujoco.mj_forward(model, data)
    rows = []
    for i in range(steps):
        # Fixed, bounded, deterministic position-actuator inputs.
        data.ctrl[:] = (0.25, -0.2, 0.15) if i < steps // 2 else (0.0, 0.1, -0.1)
        mujoco.mj_step(model, data)
        rows.append(tuple(float(v) for v in data.qpos) +
                    tuple(float(v) for v in data.qvel) +
                    tuple(float(v) for v in data.site_xpos.flatten()) + (float(data.time),))
    return rows


def fingerprint(rows: list[tuple[float, ...]]) -> str:
    h = hashlib.sha256()
    for row in rows:
        for value in row:
            h.update(struct.pack('!d', value))
    return h.hexdigest()


def measure(seed: int = 7, steps: int = 100, runs: int = 3) -> dict:
    import mujoco
    if steps < 1 or runs < 2 or steps > 10000 or runs > 100:
        raise ValueError('steps must be 1..10000 and runs 2..100')
    hashes = [fingerprint(trajectory(seed, steps)) for _ in range(runs)]
    return {'python': platform.python_version(), 'mujoco': mujoco.__version__,
            'platform': platform.platform(), 'seed': seed, 'seed_note': 'metadata only; no stochastic elements',
            'steps': steps, 'runs': runs, 'sha256': hashes,
            'exact_within_environment': len(set(hashes)) == 1,
            'cross_version': 'not tested by this run', 'cross_machine': 'not tested by this run'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--steps', type=int, default=100)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = measure(args.seed, args.steps, args.runs)
    payload = json.dumps(report, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + '\n', encoding='utf-8')
    return 0 if report['exact_within_environment'] else 1


if __name__ == '__main__':
    sys.exit(main())
