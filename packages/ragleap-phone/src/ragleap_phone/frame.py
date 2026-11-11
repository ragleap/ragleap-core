"""The audio frame that every phone adapter hands to the rest of the library."""

import array
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    """One chunk of mono audio: PCM 16-bit little-endian bytes, the sample rate they are at, and a timestamp.

    The sample rate travels with the frame because backends differ (8000, 16000, 48000), so no rate is assumed.
    timestamp_ns is a monotonic clock reading in nanoseconds, set by the adapter, so latency can be measured.
    """

    pcm: bytes
    sample_rate: int
    timestamp_ns: int

    def __post_init__(self):
        if not isinstance(self.pcm, bytes):
            raise TypeError("pcm must be bytes")
        if len(self.pcm) % 2:
            raise ValueError("pcm must hold whole 16-bit samples")
        if isinstance(self.sample_rate, bool) or not isinstance(self.sample_rate, int) or self.sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        if isinstance(self.timestamp_ns, bool) or not isinstance(self.timestamp_ns, int) or self.timestamp_ns < 0:
            raise ValueError("timestamp_ns must be a non-negative integer")

    @property
    def n_samples(self):
        """Number of 16-bit samples in the frame."""
        return len(self.pcm) // 2

    def to_samples(self):
        """The samples as array('h') in the machine's byte order."""
        out = array.array("h")
        out.frombytes(self.pcm)
        if sys.byteorder == "big":
            out.byteswap()
        return out

    @property
    def duration_s(self):
        """Length of the frame in seconds."""
        return self.n_samples / self.sample_rate
