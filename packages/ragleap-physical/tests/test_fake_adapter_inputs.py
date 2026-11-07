"""The one-joint and two-link stand-in adapters refuse command values that are not finite numbers."""
import pytest

from ragleap_physical.adapters.fake import FakeArmAdapter, FakeJointAdapter

BAD = [float("nan"), float("inf"), float("-inf"), "0.1", True, None, 10**400]
CASES = [(FakeJointAdapter, "joint_1"), (FakeArmAdapter, "joint_1"), (FakeArmAdapter, "joint_2")]


@pytest.mark.parametrize("bad", BAD)
@pytest.mark.parametrize("cls,target", CASES)
def test_apply_refuses_a_value_that_is_not_a_finite_number_and_changes_nothing(cls, target, bad):
    adapter = cls()
    before = repr(adapter.target)
    with pytest.raises(ValueError):
        adapter.apply(target, "set_position_target", bad)
    assert repr(adapter.target) == before
    assert adapter.applied == []


@pytest.mark.parametrize("bad", BAD)
@pytest.mark.parametrize("cls,target", CASES)
def test_dry_run_refuses_a_value_that_is_not_a_finite_number(cls, target, bad):
    adapter = cls()
    with pytest.raises(ValueError):
        adapter.dry_run(target, "set_position_target", bad, 0.05, 20)


@pytest.mark.parametrize("cls,target", CASES)
def test_dry_run_refuses_an_unknown_target_or_command(cls, target):
    adapter = cls()
    with pytest.raises(ValueError):
        adapter.dry_run("not_a_joint", "set_position_target", 0.1, 0.05, 20)
    with pytest.raises(ValueError):
        adapter.dry_run(target, "not_a_command", 0.1, 0.05, 20)


@pytest.mark.parametrize("cls,target", CASES)
def test_a_good_value_still_works_and_an_integer_is_accepted(cls, target):
    adapter = cls()
    adapter.apply(target, "set_position_target", 1)
    assert adapter.applied[-1] == (target, "set_position_target", 1.0)
    assert adapter.dry_run(target, "set_position_target", 0.2, 0.05, 20).reached_time
