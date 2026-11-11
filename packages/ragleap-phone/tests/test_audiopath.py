import array
import math
import random

import pytest

from ragleap_phone import Frame, InboundAudio, OutboundAudio, LAWS
from ragleap_phone import g711


def _rms(x):
    return math.sqrt(sum(v * v for v in x) / len(x))


def _tone(f, fs, n, a=10000):
    return array.array("h", [int(a * math.sin(2 * math.pi * f * i / fs)) for i in range(n)])


def _payload(n, seed=1):
    rnd = random.Random(seed)
    return bytes(rnd.randrange(256) for _ in range(n))


def _inbound(law, payload, size=None, out_rate=16000):
    ia = InboundAudio(law, out_rate)
    out = array.array("h")
    step = size or len(payload)
    for i in range(0, len(payload), step):
        out.extend(ia.process(payload[i:i + step], i).to_samples())
    out.extend(ia.finish().to_samples())
    return out


def _outbound(law, samples, size=None, in_rate=16000):
    oa = OutboundAudio(law, in_rate)
    out = bytearray()
    step = size or len(samples)
    for i in range(0, len(samples), step):
        out += oa.process(Frame(samples[i:i + step].tobytes(), in_rate, i))
    out += oa.finish()
    return bytes(out)


def test_laws():
    assert LAWS == ("ulaw", "alaw")


@pytest.mark.parametrize("law", ["mulaw", "", None, "ULAW"])
def test_unknown_laws_are_refused(law):
    with pytest.raises(ValueError):
        InboundAudio(law)
    with pytest.raises(ValueError):
        OutboundAudio(law)


@pytest.mark.parametrize("law", LAWS)
@pytest.mark.parametrize("size", [1, 7, 160, 333])
def test_inbound_chunking_does_not_change_the_samples(law, size):
    p = _payload(1001)
    assert _inbound(law, p, size) == _inbound(law, p)


@pytest.mark.parametrize("law", LAWS)
@pytest.mark.parametrize("size", [1, 7, 320, 333])
def test_outbound_chunking_does_not_change_the_bytes(law, size):
    x = array.array("h", [v for v in _tone(700, 16000, 1601)])
    assert _outbound(law, x, size) == _outbound(law, x)


@pytest.mark.parametrize("law", LAWS)
def test_lengths(law):
    p = _payload(160)
    assert len(_inbound(law, p)) == 320
    assert len(_inbound(law, p, out_rate=8000)) == 160
    x = _tone(1000, 16000, 320)
    assert len(_outbound(law, x)) == 160
    assert len(_outbound(law, _tone(1000, 8000, 160), in_rate=8000)) == 160


@pytest.mark.parametrize("law,decode", [("ulaw", g711.ulaw_decode), ("alaw", g711.alaw_decode)])
def test_inbound_at_8k_is_plain_decoding(law, decode):
    p = _payload(160)
    assert _inbound(law, p, out_rate=8000) == decode(p)


@pytest.mark.parametrize("law", LAWS)
def test_a_tone_survives_encode_then_decode(law):
    x = _tone(1000, 16000, 4800)
    back = _inbound(law, _outbound(law, x))
    k = 400
    assert _rms(back[k:-k]) / _rms(x[k:-k]) > 0.97


def test_inbound_frames_carry_the_given_timestamp_and_rate():
    f = InboundAudio("ulaw").process(_payload(160), 777)
    assert f.sample_rate == 16000 and f.timestamp_ns == 777


def test_bad_inputs_are_refused():
    with pytest.raises(TypeError):
        InboundAudio("ulaw").process("not bytes", 0)
    with pytest.raises(ValueError):
        OutboundAudio("ulaw", in_rate=48000)
    oa = OutboundAudio("alaw")
    with pytest.raises(ValueError):
        oa.process(Frame(bytes(320), 8000, 0))
