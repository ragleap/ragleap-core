import pytest

from ragleap_physical import ConfigError, Envelope

from helpers import BASE, cfg


def test_valid_envelope_has_stable_digest():
    assert Envelope(BASE).digest == Envelope(cfg()).digest


def test_digest_changes_when_limits_change():
    other = cfg(rate={"max_commands_per_s": 4})
    assert Envelope(other).digest != Envelope(BASE).digest


def test_unknown_key_refused_not_ignored():
    with pytest.raises(ConfigError):
        Envelope(cfg(workspace={"x": [-1, 1]}))


def test_clamping_not_allowed():
    with pytest.raises(ConfigError):
        Envelope(cfg(on_violation="clamp"))


def test_unsupported_command_refused():
    with pytest.raises(ConfigError):
        Envelope(cfg(allowed_commands=["format_disk"]))


def test_impossible_limits_refused():
    bad = cfg(targets={"joint_1": {"pos_min": 1.0, "pos_max": -1.0, "vel_max": 2.0, "force_max": 5.0}})
    with pytest.raises(ConfigError):
        Envelope(bad)
    bad = cfg(targets={"joint_1": {"pos_min": -1.0, "pos_max": 1.0, "vel_max": float("nan"), "force_max": 5.0}})
    with pytest.raises(ConfigError):
        Envelope(bad)


def test_view_is_a_copy():
    env = Envelope(BASE)
    view = env.as_dict()
    view["targets"]["joint_1"]["pos_max"] = 999.0
    assert env.as_dict()["targets"]["joint_1"]["pos_max"] == 1.0


def test_caller_cannot_change_envelope_through_input_dict():
    c = cfg()
    env = Envelope(c)
    c["targets"]["joint_1"]["pos_max"] = 999.0
    assert env.as_dict()["targets"]["joint_1"]["pos_max"] == 1.0
