import array
import warnings

import pytest

from ragleap_phone import g711

ALL_SAMPLES = array.array("h", range(-32768, 32768))
ALL_CODES = bytes(range(256))


def _audioop():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pytest.importorskip("audioop")


def test_table_sizes():
    assert len(g711._DEC) == 256 and len(g711._ENC) == 65536
    assert len(g711._ALAW_DEC) == 256


def test_known_mulaw_values():
    assert g711.ulaw_decode(b"\xff")[0] == 0
    assert g711.ulaw_decode(b"\x00")[0] == -32124
    assert g711.ulaw_decode(b"\x80")[0] == 32124


def test_alaw_decode_is_symmetric():
    d = g711.alaw_decode(ALL_CODES)
    assert all(d[c] == -d[c ^ 0x80] for c in range(256))


@pytest.mark.parametrize("enc,dec,rel,small", [
    (g711.ulaw_encode, g711.ulaw_decode, 0.04, 40),
    (g711.alaw_encode, g711.alaw_decode, 0.04, 40),
])
def test_round_trip_error_is_bounded(enc, dec, rel, small):
    rt = dec(enc(ALL_SAMPLES))
    assert max(abs(r - s) / abs(s) for r, s in zip(rt, ALL_SAMPLES) if abs(s) >= 1000) <= rel
    assert max(abs(r - s) for r, s in zip(rt, ALL_SAMPLES) if abs(s) < 1000) <= small


def test_alaw_encode_refuses_bad_samples():
    with pytest.raises(TypeError):
        g711.alaw_encode([1.5])
    with pytest.raises(TypeError):
        g711.alaw_encode([True])
    with pytest.raises(ValueError):
        g711.alaw_encode([40000])
    with pytest.raises(TypeError):
        g711.alaw_decode("abc")


def test_decoders_equal_audioop_for_every_code():
    ao = _audioop()
    assert g711.ulaw_decode(ALL_CODES).tobytes() == ao.ulaw2lin(ALL_CODES, 2)
    assert g711.alaw_decode(ALL_CODES).tobytes() == ao.alaw2lin(ALL_CODES, 2)


def test_encoder_differences_from_audioop_are_the_documented_ones():
    ao = _audioop()
    mu = sum(a != b for a, b in zip(g711.ulaw_encode(ALL_SAMPLES), ao.lin2ulaw(ALL_SAMPLES.tobytes(), 2)))
    al = sum(a != b for a, b in zip(g711.alaw_encode(ALL_SAMPLES), ao.lin2alaw(ALL_SAMPLES.tobytes(), 2)))
    assert (mu, al) == (381, 127)


def test_g711_matches_ragleap_voice_when_it_is_installed():
    audio = pytest.importorskip("ragleap_voice.audio")
    names = ("ulaw_encode", "ulaw_decode", "alaw_encode", "alaw_decode")
    if not all(hasattr(audio, n) for n in names):
        pytest.skip("this ragleap_voice has no G.711 helpers")
    assert g711.ulaw_encode(ALL_SAMPLES) == audio.ulaw_encode(ALL_SAMPLES)
    assert g711.alaw_encode(ALL_SAMPLES) == audio.alaw_encode(ALL_SAMPLES)
    assert g711.ulaw_decode(ALL_CODES) == audio.ulaw_decode(ALL_CODES)
    assert g711.alaw_decode(ALL_CODES) == audio.alaw_decode(ALL_CODES)
