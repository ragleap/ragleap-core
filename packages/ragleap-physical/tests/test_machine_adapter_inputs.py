"""FakeMachineToolAdapter refuses command values that are not finite numbers, in apply() and in dry_run().

This is defence in depth: the Bridge never lets such a value through (Proposal rejects it). The adapter must not rely on that.
Same rule as the one-joint and two-link adapters in fake.py. No pytest features are used, so these also run on a plain runner.
"""
import copy
from decimal import Decimal
from fractions import Fraction

import ragleap_physical.adapters.fake as fake_module
import ragleap_physical.adapters.machine as machine_module
from ragleap_physical.adapters import FakeMachineToolAdapter

BAD = [float("nan"), float("inf"), float("-inf"), "0.1", "", True, False, None, 10 ** 400, -10 ** 400, 10 ** 309, 2 ** 1024,
       Decimal("0.1"), Fraction(1, 2), complex(1, 0), b"1", [1.0], (1.0,), {"v": 1.0}]
GOOD = [0, 1, -3, 2.5, -2.5, 0.0, -0.0, 10 ** 300, 1e-300]
TOOL, CMD = "tool_1", "set_tool_offset"


def raises_value_error(fn):
    try:
        fn()
    except ValueError:
        return True
    except Exception as exc:  # any other error type is a failure of the contract (ValueError)
        raise AssertionError("expected ValueError, got " + type(exc).__name__)
    return False


def snapshot(adapter):
    return (copy.deepcopy(adapter.observe_raw()), list(adapter.applied), adapter.offset, adapter.since_change, adapter.stopped)


def test_apply_refuses_every_bad_value_with_value_error_and_changes_nothing():
    for bad in BAD:
        adapter = FakeMachineToolAdapter(seed=1)
        before = snapshot(adapter)
        assert raises_value_error(lambda: adapter.apply(TOOL, CMD, bad)), repr(bad)
        assert snapshot(adapter) == before, repr(bad)
        assert adapter.applied == [] and adapter.offset == 0.0 and adapter.since_change == 1.0e6


def test_apply_changes_nothing_after_a_refusal_even_when_the_tool_was_already_set():
    adapter = FakeMachineToolAdapter(seed=1)
    adapter.apply(TOOL, CMD, 2.0)
    before = snapshot(adapter)
    for bad in BAD:
        assert raises_value_error(lambda: adapter.apply(TOOL, CMD, bad)), repr(bad)
        assert snapshot(adapter) == before, repr(bad)
    assert adapter.applied == [(TOOL, CMD, 2.0)] and adapter.offset == 2.0 and adapter.since_change == 0.0


def test_dry_run_refuses_every_bad_value_with_value_error_and_changes_nothing():
    for bad in BAD:
        adapter = FakeMachineToolAdapter(seed=1)
        before = snapshot(adapter)
        assert raises_value_error(lambda: adapter.dry_run(TOOL, CMD, bad, 5.0, 100)), repr(bad)
        assert snapshot(adapter) == before, repr(bad)


def test_integers_and_floats_still_work_and_are_stored_as_floats():
    for good in GOOD:
        adapter = FakeMachineToolAdapter(seed=1)
        pred = adapter.dry_run(TOOL, CMD, good, 5.0, 100)
        assert pred.final_pos == float(good) and isinstance(pred.final_pos, float) and pred.reached_time
        assert adapter.applied == [] and adapter.offset == 0.0  # a dry run changes nothing
        adapter.apply(TOOL, CMD, good)
        assert adapter.offset == float(good) and isinstance(adapter.offset, float), repr(good)
        assert adapter.applied == [(TOOL, CMD, float(good))] and isinstance(adapter.applied[0][2], float)
        assert adapter.since_change == 0.0


def test_an_unknown_target_or_command_is_still_refused_first():
    adapter = FakeMachineToolAdapter(seed=1)
    for target, command in (("joint_1", CMD), (TOOL, "set_position_target"), (TOOL, "change_tool"), (TOOL, "")):
        assert raises_value_error(lambda: adapter.apply(target, command, 1.0))
        assert raises_value_error(lambda: adapter.dry_run(target, command, 1.0, 5.0, 100))
        assert raises_value_error(lambda: adapter.apply(target, command, float("nan")))
    assert adapter.applied == [] and adapter.offset == 0.0


def test_a_refused_value_does_not_disturb_the_simulation_that_follows():
    clean, hit = FakeMachineToolAdapter(seed=7), FakeMachineToolAdapter(seed=7)
    for bad in BAD:
        raises_value_error(lambda: hit.apply(TOOL, CMD, bad))
        raises_value_error(lambda: hit.dry_run(TOOL, CMD, bad, 5.0, 100))
    for adapter in (clean, hit):
        adapter.advance(40.0, 1000)
    assert clean.observe_raw() == hit.observe_raw()


def outcome(fn, value):
    try:
        return fn(value)
    except ValueError:
        return "refused"


def same(a, b):
    return a == b or (a != a and b != b)


def test_the_check_is_the_same_as_in_the_other_stand_in_adapters():
    """machine.py carries its own copy of the check (the published fake.py may not have it yet). Where fake.py has it, both must agree."""
    mine = machine_module._finite_value
    for value in BAD:
        assert outcome(mine, value) == "refused", repr(value)
    for value in GOOD:
        result = outcome(mine, value)
        assert result == float(value) and isinstance(result, float), repr(value)
    reference = getattr(fake_module, "_finite_value", None)
    if reference is not None:
        for value in BAD + GOOD:
            a, b = outcome(mine, value), outcome(reference, value)
            assert type(a) is type(b) and same(a, b), repr(value)
