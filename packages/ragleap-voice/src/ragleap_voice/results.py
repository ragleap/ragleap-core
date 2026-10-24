"""Result types of the planned API (docs/design/api.md section 2). Standard library only.

Every result says which engine and model produced it and the verification status of that model.
"""
import pathlib
from dataclasses import dataclass
from typing import Optional, Tuple

MODEL_STATUSES = ("verified", "unverified")


def _notes(value):
    if isinstance(value, str) or not all(isinstance(x, str) for x in value):
        raise TypeError("notes must be a sequence of str")
    return tuple(value)


def _check_status(status):
    if status not in MODEL_STATUSES:
        raise ValueError("model_status must be one of %s, got %r" % (", ".join(MODEL_STATUSES), status))


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str

    def __post_init__(self):
        if self.start < 0 or self.end < self.start:
            raise ValueError("a segment needs 0 <= start <= end, got %r and %r" % (self.start, self.end))


@dataclass(frozen=True)
class Transcript:
    text: str
    language: Optional[str]
    engine: str
    model: str
    model_status: str
    segments: Tuple[Segment, ...] = ()
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        _check_status(self.model_status)
        object.__setattr__(self, "notes", _notes(self.notes))
        segs = tuple(self.segments)
        if not all(isinstance(s, Segment) for s in segs):
            raise TypeError("segments must contain only Segment objects")
        object.__setattr__(self, "segments", segs)


@dataclass(frozen=True)
class Speech:
    wav: bytes
    sample_rate: int
    duration_s: float
    engine: str
    model: str
    model_status: str
    voice: Optional[str] = None
    synthetic: bool = True
    watermarked: bool = False
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        _check_status(self.model_status)
        if self.synthetic is not True:
            raise ValueError("synthetic must be True: every Speech is marked as synthetic")
        if not isinstance(self.sample_rate, int) or self.sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        if self.duration_s < 0:
            raise ValueError("duration_s must not be negative")
        object.__setattr__(self, "notes", _notes(self.notes))

    def save(self, path, *, overwrite=False):
        """Write the WAV bytes to path and return it. Refuses to replace an existing file unless overwrite is true."""
        p = pathlib.Path(path)
        with open(p, "wb" if overwrite else "xb") as fh:
            fh.write(self.wav)
        return p
