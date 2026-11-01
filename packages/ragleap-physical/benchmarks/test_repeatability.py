"""Benchmark contract tests (simulation only)."""
import importlib.util
from pathlib import Path
import pytest

pytest.importorskip('mujoco')
spec = importlib.util.spec_from_file_location('b002_repeatability', Path(__file__).with_name('repeatability.py'))
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_repeated_trajectory_exact():
    report = benchmark.measure(seed=7, steps=30, runs=3)
    assert report['exact_within_environment'] is True
    assert len(set(report['sha256'])) == 1


def test_trajectory_changes_with_steps():
    assert benchmark.fingerprint(benchmark.trajectory(7, 30)) != benchmark.fingerprint(benchmark.trajectory(7, 31))


def test_bounded_runs():
    with pytest.raises(ValueError):
        benchmark.measure(steps=0)
    with pytest.raises(ValueError):
        benchmark.measure(runs=1)
