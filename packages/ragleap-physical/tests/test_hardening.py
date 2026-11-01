"""Regression tests for the hardening round (findings F01 to F06 and findings a to f of the independent reviews). Standard library only."""
import json
import math

import pytest

from ragleap_physical import (ApprovalError, Bridge, Capability, ConfigError, Envelope, EnvelopeViolation, ExpectedOutcome, Observation,
                              PhysicalTools, ValidationError)
from ragleap_physical.adapters import DryRun, FakeArmAdapter, FakeJointAdapter
from ragleap_physical.mcp_server import make_server
from ragleap_physical.observation import READ_LIMIT, actor, finite, ident

from helpers import BASE, Clock, cfg
from test_mcp_server import Running, post, rpc, setup
from test_workspace import BOX, JOINT, arm_cfg

INF, NAN = math.inf, math.nan


def code_of(fn, *a):
    try:
        fn(*a)
    except EnvelopeViolation as exc:
        return exc.code
    return None


def build(raw):
    return Observation.from_raw(raw, org_id="org_demo", observation_id="obs_000001")


def dry(vel=1.0, force=1.0, per_joint=None, **kw):
    return DryRun(0.5, vel, force, True, per_joint, **kw)


# ---- F01: invalid dry-run predictions
def test_f01_nan_inf_and_negative_predictions_are_refused():
    env = Envelope(BASE)
    for vel, force in ((NAN, 1.0), (-INF, 1.0), (1.0, NAN), (1.0, -1.0), (-0.5, 1.0), (INF, 1.0), (1.0, INF)):
        assert code_of(env.check_dry_run, "joint_1", dry(vel, force)) is not None, (vel, force)
    assert code_of(env.check_dry_run, "joint_1", dry(1.0, 1.0)) is None


def test_f01_non_numbers_and_bools_are_refused():
    env = Envelope(BASE)
    for bad in ("1", None, True, [1]):
        assert code_of(env.check_dry_run, "joint_1", dry(bad, 1.0)) == "prediction_invalid"


def test_f01_per_joint_values_are_checked():
    env = Envelope(arm_cfg())
    for pair in ((NAN, 1.0), (1.0, -1.0), (-INF, 1.0), (1.0, NAN), (1.0,), "ab", None):
        pj = {"joint_1": pair, "joint_2": (1.0, 1.0)}
        assert code_of(env.check_dry_run, "joint_1", dry(per_joint=pj, site_min=(0.5, 0, 0.5), site_max=(0.5, 0, 0.5))) == "prediction_invalid", pair


def test_f01_check_prediction_is_also_strict():
    env = Envelope(BASE)
    for vel, force in ((NAN, 1.0), (1.0, NAN), (-1.0, 1.0), (1.0, -1.0)):
        assert code_of(env.check_prediction, "joint_1", vel, force) == "prediction_invalid"
    assert code_of(env.check_prediction, "joint_1", 1.0, 1.0) is None


# ---- F04: empty per_joint hid bad aggregate values
def test_f04_empty_per_joint_does_not_hide_bad_aggregates():
    env = Envelope(BASE)
    for vel, force in ((INF, 1.0), (1.0, INF), (NAN, 1.0), (1.0, NAN)):
        assert code_of(env.check_dry_run, "joint_1", dry(vel, force, per_joint={})) is not None
    # an empty mapping falls through to the aggregate limit check
    assert code_of(env.check_dry_run, "joint_1", dry(99.0, 1.0, per_joint={})) == "vel"


def test_f04_bad_aggregate_is_refused_even_when_per_joint_looks_fine():
    env = Envelope(arm_cfg())
    pj = {"joint_1": (1.0, 1.0), "joint_2": (1.0, 1.0)}
    for vel, force in ((INF, 1.0), (1.0, NAN), (NAN, NAN)):
        d = dry(vel, force, per_joint=pj, site_min=(0.5, 0, 0.5), site_max=(0.5, 0, 0.5))
        assert code_of(env.check_dry_run, "joint_1", d) == "prediction_invalid"


def test_non_finite_predicted_positions_are_refused():
    env = Envelope(arm_cfg())
    pj = {"joint_1": (1.0, 1.0), "joint_2": (1.0, 1.0)}
    for lo, hi in (((NAN, 0, 0.5), (0.5, 0, 0.5)), ((0.5, 0, 0.5), (0.5, NAN, 0.5)), ((0.5, 0), (0.5, 0, 0.5)), ("abc", "def")):
        assert code_of(env.check_dry_run, "joint_1", dry(per_joint=pj, site_min=lo, site_max=hi)) == "prediction_invalid", (lo, hi)


# ---- F02: an incomplete tracked point must not be ignored
def _sensors(*items):
    return [{"id": i, "kind": "site_position", "value": v, "unit": "m"} for i, v in items]


def _arm_obs(extra):
    raw = dict(FakeArmAdapter().observe_raw())
    raw["sensors"] = _sensors(("tip_x", 0.5), ("tip_y", 0.0), ("tip_z", 0.6)) + extra
    return build(raw)


def test_f02_incomplete_second_point_is_refused():
    env = Envelope(arm_cfg())
    env.check_workspace(_arm_obs([]))  # control: one complete point passes
    for extra in (_sensors(("b_x", 0.5), ("b_y", 0.0)), _sensors(("b_x", 0.5)), _sensors(("b_z", 0.6))):
        assert code_of(env.check_workspace, _arm_obs(extra)) == "workspace_unknown"


def test_f02_incomplete_point_cannot_hide_a_point_outside():
    env = Envelope(arm_cfg())
    obs = _arm_obs(_sensors(("far_x", 50.0), ("far_y", 0.0)))  # z missing: used to be skipped
    assert code_of(env.check_workspace, obs) == "workspace_unknown"


def test_f02_no_position_at_all_is_still_refused():
    env = Envelope(arm_cfg())
    raw = dict(FakeArmAdapter().observe_raw())
    raw["sensors"] = []
    assert code_of(env.check_workspace, build(raw)) == "workspace_unknown"


# ---- F03: the Bridge constructor checks its numbers
def _bridge(**kw):
    return Bridge(FakeJointAdapter(), Envelope(BASE), org_id="org_demo", max_capability=Capability.CONTROL, clock=Clock(), **kw)


def test_f03_bad_ttl_is_refused():
    for bad in (0, -1, INF, -INF, NAN, True, "300", None):
        with pytest.raises(ConfigError):
            _bridge(ttl_s=bad)
    _bridge(ttl_s=60)


def test_f03_bad_chunk_size_is_refused():
    for bad in (0, -0.1, INF, NAN, True, "1", None):
        with pytest.raises(ConfigError):
            _bridge(chunk_s=bad)
    _bridge(chunk_s=0.5)


def test_f03_caps_must_be_whole_positive_numbers():
    for name in ("max_pending", "max_records", "max_audit"):
        for bad in (0, -1, 1.5, INF, NAN, True, "5", None):
            with pytest.raises(ConfigError):
                _bridge(**{name: bad})
        _bridge(**{name: 3})


# ---- findings (a) to (f)
def test_a_trailing_newline_is_refused_in_identifiers_and_actors():
    for fn, good in ((ident, "joint_1"), (actor, "person:alice")):
        assert fn(good) == good
        for bad in (good + "\n", good + "\r\n", "\n" + good):
            with pytest.raises(ValidationError):
                fn(bad)


def test_b_huge_integers_become_a_validation_error():
    for bad in (10 ** 400, -(10 ** 400)):
        with pytest.raises(ValidationError):
            finite(bad)
    assert finite(3) == 3.0


def test_c_the_size_cap_counts_invalid_items_too():
    bad = {"id": "has space", "kind": "joint", "pos": 0.0, "vel": 0.0, "pos_unit": "rad", "vel_unit": "rad/s"}
    raw = dict(FakeJointAdapter().observe_raw())
    raw["entities"] = [dict(bad) for _ in range(100000)]
    o = build(raw)
    assert o.truncated is True and o.dropped <= READ_LIMIT and o.entities == ()


def test_d_duplicate_ids_are_all_dropped():
    base = dict(FakeJointAdapter().observe_raw()["entities"][0])
    raw = dict(FakeJointAdapter().observe_raw())
    raw["entities"] = [dict(base, id="dup1", pos=0.1), dict(base, id="dup1", pos=0.9), dict(base, id="solo", pos=0.2)]
    o = build(raw)
    assert [e.id for e in o.entities] == ["solo"] and o.dropped >= 2
    sens = [{"id": "s1", "kind": "k", "value": 1.0, "unit": "1"}, {"id": "s1", "kind": "k", "value": 2.0, "unit": "1"}]
    raw["sensors"] = sens
    assert build(raw).sensors == ()


def test_e_control_and_bidi_characters_are_refused_in_reason():
    b, adapter, _ = _make_basic()
    for bad in ("a\x1b[31mred", "line1\nline2", "x\u202eevil", "x\u2066y", "x\x00y", "x\u200bhidden"):
        with pytest.raises(ValidationError):
            _propose(b, reason=bad)
    _propose(b, reason="move to 0.5 rad, as discussed")
    _propose(b, reason="\u0915\u094d\u200d\u0937")  # ZWJ inside Indic script stays allowed


def test_f_deeply_nested_json_gets_an_answer_not_silence():
    t, _, _ = setup()
    deep = b"[" * 30000 + b"]" * 30000
    with Running(t) as s:
        st, _ = post(s.url, None, raw=deep)
        assert st == 400
        assert post(s.url, rpc("ping"))[0] == 200  # server still alive


def _make_basic():
    b = _bridge()
    return b, b._adapter, None


def _propose(b, **kw):
    return b.propose(requested_by="employee:ops", target="joint_1", command="set_position_target", value=0.5, unit="rad",
                     expected_outcome=ExpectedOutcome("joint_1", "pos", 0.45, 0.55, "rad", 5.0), **kw)


# ---- observe-only envelope
class ReadOnlyJoint(FakeJointAdapter):
    def targets(self):
        return None  # an adapter that cannot move anything lists no joints


def test_observe_only_bridge_reads_but_never_proposes():
    b = Bridge(ReadOnlyJoint(), None, org_id="org_demo", max_capability=Capability.ANALYSE, clock=Clock())
    assert b.observe().entities
    with pytest.raises(Exception):
        _propose(b)
    with pytest.raises(ConfigError):
        Bridge(FakeJointAdapter(), None, org_id="org_demo", max_capability=Capability.CONTROL, clock=Clock())
    with pytest.raises(ConfigError):
        Bridge(FakeJointAdapter(), Envelope.observe_only(), org_id="org_demo", max_capability=Capability.RECOMMEND, clock=Clock())


def test_new_units_are_accepted():
    base = dict(FakeJointAdapter().observe_raw())
    base["sensors"] = [{"id": "p1", "kind": "power", "value": 2.5, "unit": "W"}, {"id": "e1", "kind": "energy", "value": 7.0, "unit": "kWh"}]
    assert len(build(base).sensors) == 2


# ---- found in the lead's own review: no self-approval
def test_the_requester_cannot_approve_its_own_proposal():
    b, adapter, _ = _make_basic()
    p = _propose(b)
    with pytest.raises(ApprovalError):
        b.approve(p, "employee:ops")  # requested_by is employee:ops
    assert adapter.applied == [] and any(r.get("code") == "self_approval" for r in b.audit)
    b.approve(p, "person:alice")  # a different named actor still works
    assert b.apply(p).state == "verified"


# ---- F05 and F06 (recheck)
def test_f05_inverted_predicted_position_bounds_are_refused():
    env = Envelope(arm_cfg())
    pj = {"joint_1": (1.0, 1.0), "joint_2": (1.0, 1.0)}
    d = dry(per_joint=pj, site_min=(0.8, 0, 0.5), site_max=(-0.8, 0, 0.5))
    assert code_of(env.check_dry_run, "joint_1", d) == "prediction_invalid"
    for lo, hi in (((0.5, 0, 0.5), (0.4999, 0, 0.5)), ((0.5, 0.01, 0.5), (0.5, 0.0, 0.5)), ((0.5, 0, 0.6), (0.5, 0, 0.5))):
        assert code_of(env.check_dry_run, "joint_1", dry(per_joint=pj, site_min=lo, site_max=hi)) == "prediction_invalid", (lo, hi)
    ok = dry(per_joint=pj, site_min=(0.4, 0, 0.5), site_max=(0.6, 0, 0.5))
    assert code_of(env.check_dry_run, "joint_1", ok) is None


def test_f06_aggregate_smaller_than_a_joint_value_is_refused():
    env = Envelope(arm_cfg())
    pos = dict(site_min=(0.5, 0, 0.5), site_max=(0.5, 0, 0.5))
    pj = {"joint_1": (1.0, 1.0), "joint_2": (0.5, 0.5)}
    for vel, force in ((0.0, 0.0), (0.9, 1.0), (1.0, 0.9)):
        assert code_of(env.check_dry_run, "joint_1", dry(vel, force, per_joint=pj, **pos)) == "prediction_invalid", (vel, force)
    assert code_of(env.check_dry_run, "joint_1", dry(1.0, 1.0, per_joint=pj, **pos)) is None
    assert code_of(env.check_dry_run, "joint_1", dry(2.0, 2.0, per_joint=pj, **pos)) is None  # a larger total is only cautious
