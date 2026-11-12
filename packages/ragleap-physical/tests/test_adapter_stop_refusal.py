"""Conformance checks that adapters refuse commands while stopped.

This is a test-only extension to the adapter conformance kit. Control adapters
use ValueError for a refused command, as specified by adapters/base.py.
Observe-only adapters retain the existing refusal set.
"""
from __future__ import annotations

import copy

import pytest

from test_adapter_conformance import CASES, _expected_refusal


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_apply_refuses_while_stopped(case):
    """A stopped adapter must refuse apply without changing its state."""
    try:
        adapter = case.factory()
    except ImportError as exc:
        pytest.skip(f"optional dependency unavailable: {exc}")

    adapter.stop()
    before = copy.deepcopy(adapter.observe_raw())
    target = case.target or "not_a_target"
    command = case.command or "not_a_command"

    with pytest.raises(_expected_refusal(case)):
        adapter.apply(target, command, case.value)

    assert adapter.observe_raw() == before

    # A control adapter becomes commandable again only after an explicit clear.
    if case.mode == "control":
        assert adapter.clear_stop() is None
        assert adapter.apply(target, command, case.value) is None
