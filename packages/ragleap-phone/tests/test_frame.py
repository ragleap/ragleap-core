import dataclasses
import struct

import pytest

from ragleap_phone import Frame


def test_samples_round_trip():
    f = Frame(struct.pack("<3h", 1, -2, 3), 8000, 5)
    assert list(f.to_samples()) == [1, -2, 3]
    assert f.n_samples == 3


def test_duration():
    f = Frame(bytes(320), 8000, 0)
    assert f.n_samples == 160
    assert f.duration_s == pytest.approx(0.02)


def test_empty_frame_is_allowed():
    f = Frame(b"", 16000, 0)
    assert f.n_samples == 0
    assert f.duration_s == 0


@pytest.mark.parametrize("pcm,rate,ts,exc", [
    (b"\x00", 8000, 0, ValueError),
    ("ab", 8000, 0, TypeError),
    (bytes(4), 0, 0, ValueError),
    (bytes(4), -8000, 0, ValueError),
    (bytes(4), 8000.0, 0, ValueError),
    (bytes(4), True, 0, ValueError),
    (bytes(4), 8000, -1, ValueError),
    (bytes(4), 8000, 1.5, ValueError),
])
def test_bad_values_are_refused(pcm, rate, ts, exc):
    with pytest.raises(exc):
        Frame(pcm, rate, ts)


def test_frame_is_immutable():
    f = Frame(bytes(4), 8000, 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.sample_rate = 16000
