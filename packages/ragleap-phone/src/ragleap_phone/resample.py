"""Sample-rate conversion for Frame objects: 16 kHz <-> 8 kHz and 16 kHz <-> 48 kHz, standard library only.

The filters are the ones of ragleap-voice's audio helpers (a windowed-sinc low-pass; 31 taps with cutoff 0.225 for 2:1, 63 taps
with cutoff 0.14 for 3:1), copied here so that ragleap-phone does not depend on ragleap-voice. Results are truncated toward
zero, like the originals. The filter delay is not compensated. Effect on recognition accuracy: measured for the 3:1 pair
on clean read speech only (see benchmarks/voice/README.md in ragleap-core), not for any real call.
"""

import array
import math
import sys
from operator import mul

from .frame import Frame


def _taps(n, fc):
    m = n // 2
    t = []
    for i in range(n):
        k = i - m
        h = 2 * fc if k == 0 else math.sin(2 * math.pi * fc * k) / (math.pi * k)
        t.append(h * (0.54 - 0.46 * math.cos(2 * math.pi * i / (n - 1))))
    total = sum(t)
    return [x / total for x in t]


def _scaled(taps, g):
    return [g * x for x in taps]


_T2 = _taps(31, 0.225)
_T3 = _taps(63, 0.14)

# (from_rate, to_rate): (taps, keep every step-th output, zero-insertion factor before the filter)
_CONFIG = {
    (16000, 8000): (_T2, 2, 1),
    (8000, 16000): (_scaled(_T2, 2), 1, 2),
    (48000, 16000): (_T3, 3, 1),
    (16000, 48000): (_scaled(_T3, 3), 1, 3),
}
SUPPORTED_RATE_PAIRS = tuple(sorted(_CONFIG))


def _clip(x):
    return max(-32768, min(32767, int(x)))


def _to_pcm(samples):
    if sys.byteorder == "big":
        samples = array.array("h", samples)
        samples.byteswap()
    return samples.tobytes()


class Resampler:
    """A stateful rate converter for a stream of Frame objects. Concatenating the output of every process() call and of
    finish() gives the same samples as processing all the input in one frame. Not thread-safe."""

    def __init__(self, from_rate, to_rate):
        for r in (from_rate, to_rate):
            if isinstance(r, bool) or not isinstance(r, int):
                raise ValueError("rates must be integers")
        self.from_rate = from_rate
        self.to_rate = to_rate
        self._identity = from_rate == to_rate
        if not self._identity:
            if (from_rate, to_rate) not in _CONFIG:
                raise ValueError("unsupported conversion %d -> %d Hz; supported: %s" % (from_rate, to_rate, list(SUPPORTED_RATE_PAIRS)))
            self._taps, self._step, self._up = _CONFIG[(from_rate, to_rate)]
            self._len = len(self._taps)
            self._buf = [0] * (self._len // 2)
        self._last_ts = 0
        self._done = False

    def _emit(self):
        buf, taps, out, pos = self._buf, self._taps, [], 0
        n = self._len
        while pos + n <= len(buf):
            out.append(_clip(sum(map(mul, taps, buf[pos:pos + n]))))
            pos += self._step
        del buf[:pos]
        return array.array("h", out)

    def process(self, frame):
        """Convert one Frame; the output holds the samples that are complete now and carries the input frame's timestamp."""
        if self._done:
            raise ValueError("process() called after finish()")
        if frame.sample_rate != self.from_rate:
            raise ValueError("frame is at %d Hz, expected %d Hz" % (frame.sample_rate, self.from_rate))
        self._last_ts = frame.timestamp_ns
        if self._identity:
            return frame
        samples = list(frame.to_samples())
        if self._up > 1:
            u = [0] * (self._up * len(samples))
            u[::self._up] = samples
            samples = u
        self._buf.extend(samples)
        return Frame(_to_pcm(self._emit()), self.to_rate, frame.timestamp_ns)

    def finish(self):
        """Flush the tail with zero padding. Returns a Frame (possibly empty). Later calls return an empty Frame."""
        if self._done or self._identity:
            self._done = True
            return Frame(b"", self.to_rate, self._last_ts)
        self._done = True
        self._buf.extend([0] * (self._len // 2))
        return Frame(_to_pcm(self._emit()), self.to_rate, self._last_ts)
