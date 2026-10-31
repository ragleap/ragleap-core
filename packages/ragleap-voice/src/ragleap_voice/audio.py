"""Audio helpers without audioop (docs/design/audio-helpers.md). Standard library only; samples are array('h').

Status: pre-alpha, not exported from the package yet.
- Mu-law decode is table-driven and identical to audioop for all 256 codes (checked in benchmarks/voice).
- The mu-law encoder is the benchmark prototype's. It is NOT verified against the G.711 standard: on all 65536 16-bit
  inputs it differs from audioop on 381 and from ffmpeg's pcm_mulaw on 851, always by one code and only for negative
  inputs. The noise pilot's telephone path used ffmpeg. Do not rely on bit-exactness with any implementation.
- The resamplers are 2:1 only: a 31-tap Hamming-windowed sinc, cutoff 0.225 cycles per input sample, unity DC gain, zero
  history at the clip edges. Their effect on recognition accuracy has not been measured.
- Down2Stream and Up2Stream are stateful versions of down2 and up2 (chunked output equals one-shot output exactly).
- A-law is not implemented.
"""
import array
import math
from operator import mul

FULL_SCALE = 32768.0
BIAS = 0x84


def _dec(u):
    u = ~u & 0xFF
    s = (((u & 0x0F) << 3) + BIAS) << ((u >> 4) & 7)
    s -= BIAS
    return -s if u & 0x80 else s


def _enc(s):
    sign = 0x80 if s < 0 else 0
    if s < 0:
        s = -s
    if s > 32635:
        s = 32635
    s += BIAS
    e = s.bit_length() - 8
    return ~(sign | (e << 4) | ((s >> (e + 3)) & 0x0F)) & 0xFF


_DEC = [_dec(u) for u in range(256)]
_ENC = bytes(_enc(i - 32768) for i in range(65536))


def ulaw_decode(codes):
    """G.711 mu-law bytes -> array('h') of 16-bit linear samples."""
    return array.array("h", [_DEC[b] for b in bytes(codes)])


def ulaw_encode(samples):
    """16-bit linear samples -> mu-law bytes (see the module note on exactness)."""
    return bytes([_ENC[s + 32768] for s in samples])


def frames(samples, size, remainder="drop"):
    """Yield consecutive frames of `size` samples. remainder: 'drop' the short tail, 'pad' it with zeros, or 'keep' it short."""
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("size must be a positive integer")
    if remainder not in ("drop", "pad", "keep"):
        raise ValueError("remainder must be 'drop', 'pad' or 'keep'")
    n = len(samples)
    for i in range(0, n - n % size, size):
        yield samples[i:i + size]
    tail = n % size
    if tail and remainder != "drop":
        t = samples[n - tail:]
        if remainder == "pad":
            t = array.array("h", t) + array.array("h", [0] * (size - tail))
        yield t


def rms(samples):
    """Root mean square in sample units (0.0 for an empty input)."""
    return math.sqrt(sum(map(mul, samples, samples)) / len(samples)) if len(samples) else 0.0


def peak(samples):
    return max((abs(s) for s in samples), default=0)


def _dbfs(x):
    return 20.0 * math.log10(x / FULL_SCALE) if x > 0 else -math.inf


def rms_dbfs(samples):
    """RMS in dBFS, full scale 32768; -inf for silence."""
    return _dbfs(rms(samples))


def peak_dbfs(samples):
    return _dbfs(peak(samples))


def _clip(x):
    return max(-32768, min(32767, int(x)))


def gain(samples, g):
    """Multiply by g with clipping."""
    return array.array("h", [_clip(x * g) for x in samples])


def normalize_level(samples, target_dbfs=-23.0, peak_limit=0.95):
    """Scale to target RMS, lowering the gain if the peak would pass peak_limit of full scale.
    Returns (samples, applied_gain_db). Silence is returned unchanged with 0.0 dB."""
    r = rms(samples)
    if r <= 0:
        return array.array("h", samples), 0.0
    g = FULL_SCALE * 10 ** (target_dbfs / 20.0) / r
    p = peak(samples)
    if p * g > peak_limit * FULL_SCALE:
        g = peak_limit * FULL_SCALE / p
    return gain(samples, g), 20.0 * math.log10(g)


def _taps(n=31, fc=0.225):
    m = n // 2
    t = []
    for i in range(n):
        k = i - m
        h = 2 * fc if k == 0 else math.sin(2 * math.pi * fc * k) / (math.pi * k)
        t.append(h * (0.54 - 0.46 * math.cos(2 * math.pi * i / (n - 1))))
    total = sum(t)
    return [x / total for x in t]


_TAPS = _taps()
_TAPS2 = [2 * x for x in _TAPS]
_L = len(_TAPS)


def down2(samples):
    """16 kHz -> 8 kHz (any rate -> half): low-pass, then keep every second sample."""
    pad = _L // 2
    xp = [0] * pad + list(samples) + [0] * pad
    return array.array("h", [_clip(sum(map(mul, _TAPS, xp[i:i + _L]))) for i in range(0, len(samples), 2)])


def up2(samples):
    """8 kHz -> 16 kHz (any rate -> double): zero insertion, then the low-pass with doubled gain."""
    pad = _L // 2
    u = [0] * (2 * len(samples))
    u[::2] = samples
    xp = [0] * pad + u + [0] * pad
    return array.array("h", [_clip(sum(map(mul, _TAPS2, xp[i:i + _L]))) for i in range(len(u))])


class _Resample2Stream:
    """Stateful 2:1 resampler. Concatenating every process() result and the finish() result equals the one-shot
    function's output exactly. Keeps less than one filter length of history. Not thread-safe."""

    _taps = None
    _step = 1

    def __init__(self):
        self._buf = [0] * (_L // 2)
        self._done = False

    def _prepare(self, samples):
        return list(samples)

    def _emit(self):
        buf, taps, out, pos = self._buf, self._taps, [], 0
        while pos + _L <= len(buf):
            out.append(_clip(sum(map(mul, taps, buf[pos:pos + _L]))))
            pos += self._step
        del buf[:pos]
        return array.array("h", out)

    def process(self, samples):
        """Accept a chunk (any length, may be empty); return the output samples that are now complete."""
        if self._done:
            raise ValueError("process() called after finish()")
        self._buf.extend(self._prepare(samples))
        return self._emit()

    def finish(self):
        """Flush the tail with the same zero padding as the one-shot function. Later calls return an empty array."""
        if self._done:
            return array.array("h")
        self._done = True
        self._buf.extend([0] * (_L // 2))
        return self._emit()


class Down2Stream(_Resample2Stream):
    """Streaming version of down2()."""

    _taps = _TAPS
    _step = 2


class Up2Stream(_Resample2Stream):
    """Streaming version of up2()."""

    _taps = _TAPS2
    _step = 1

    def _prepare(self, samples):
        samples = list(samples)
        u = [0] * (2 * len(samples))
        u[::2] = samples
        return u
