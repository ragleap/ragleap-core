"""Engine interface of the planned API (docs/design/api.md section 4). Standard library only.

An engine is handed directories of verified model files and never fetches anything itself. This module holds data types and a
protocol only; no real engine exists yet.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Tuple

from .results import Segment

KINDS = ("stt", "tts")


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError("kind must be one of %s, got %r" % (", ".join(KINDS), kind))


def _strings(value, label):
    if isinstance(value, str):
        raise TypeError(label + " must be a collection of str, not one str")
    items = frozenset(value)
    if not all(isinstance(x, str) for x in items):
        raise TypeError(label + " must contain only str")
    return items


@dataclass(frozen=True)
class Capabilities:
    kind: str
    languages: frozenset = frozenset()
    clone: bool = False
    emotion: frozenset = frozenset()
    emotion_languages: frozenset = frozenset()
    streaming: bool = False
    hardware: frozenset = frozenset({"cpu"})
    watermarks_output: bool = False
    isolated: bool = False

    def __post_init__(self):
        _check_kind(self.kind)
        for label in ("languages", "emotion", "emotion_languages", "hardware"):
            object.__setattr__(self, label, _strings(getattr(self, label), label))


@dataclass(frozen=True)
class Request:
    kind: str
    language: Optional[str] = None
    audio: Optional[bytes] = None
    text: Optional[str] = None
    voice: Optional[str] = None
    emotion: Optional[str] = None
    speed: Optional[float] = None
    timestamps: bool = False
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _check_kind(self.kind)
        if self.kind == "stt" and not self.audio:
            raise ValueError("a speech-to-text request needs audio bytes")
        if self.kind == "tts" and not (self.text and self.text.strip()):
            raise ValueError("a text-to-speech request needs text")
        object.__setattr__(self, "options", dict(self.options))


@dataclass(frozen=True)
class Result:
    text: Optional[str] = None
    segments: Tuple[Segment, ...] = ()
    wav: Optional[bytes] = None
    sample_rate: Optional[int] = None
    duration_s: Optional[float] = None
    language: Optional[str] = None
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        segs = tuple(self.segments)
        if not all(isinstance(s, Segment) for s in segs):
            raise TypeError("segments must contain only Segment objects")
        object.__setattr__(self, "segments", segs)
        if isinstance(self.notes, str) or not all(isinstance(x, str) for x in self.notes):
            raise TypeError("notes must be a sequence of str")
        object.__setattr__(self, "notes", tuple(self.notes))


@dataclass(frozen=True)
class EngineInfo:
    name: str
    kind: str
    capabilities: Capabilities
    manifest_entries: Tuple[str, ...]
    third_party: bool


class Engine(Protocol):
    name: str
    manifest_entries: Tuple[str, ...]
    capabilities: Capabilities

    def load(self, model_dirs: Mapping[str, Path]) -> None: ...

    def run(self, request: Request) -> Result: ...

    def close(self) -> None: ...
