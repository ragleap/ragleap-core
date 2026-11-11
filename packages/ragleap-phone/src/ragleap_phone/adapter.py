"""The provider adapter contract (docs/design/phase0-interface.md, section 3). Interfaces only: no backend, no calls."""

import abc
import secrets
from dataclasses import dataclass

EVENT_KINDS = ("call.started", "dtmf", "playback.finished", "call.ended", "error")
DTMF_DIGITS = "0123456789*#ABCD"
DIRECTIONS = ("inbound", "outbound")


def new_call_id():
    """A call id made on our side. An id sent by the remote party must never be used as a call id."""
    return secrets.token_hex(8)


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


@dataclass(frozen=True)
class Capabilities:
    """What an adapter can do: its transport, the codecs and sample rates it handles, DTMF, barge-in and a call limit."""

    transport: str
    codecs: tuple
    sample_rates: tuple
    dtmf: bool
    barge_in: bool
    max_concurrent_calls: int

    def __post_init__(self):
        if not isinstance(self.transport, str) or not self.transport:
            raise ValueError("transport must be a non-empty string")
        if not isinstance(self.codecs, tuple) or not self.codecs or not all(isinstance(c, str) and c for c in self.codecs):
            raise ValueError("codecs must be a non-empty tuple of strings")
        if not isinstance(self.sample_rates, tuple) or not self.sample_rates or not all(_is_int(r) and r > 0 for r in self.sample_rates):
            raise ValueError("sample_rates must be a non-empty tuple of positive integers")
        if not isinstance(self.dtmf, bool) or not isinstance(self.barge_in, bool):
            raise ValueError("dtmf and barge_in must be booleans")
        if not _is_int(self.max_concurrent_calls) or self.max_concurrent_calls < 1:
            raise ValueError("max_concurrent_calls must be an integer of at least 1")


@dataclass(frozen=True)
class Event:
    """One call event. For a dtmf event, data is the digit. For an error event, data is a short message that must never hold
    audio, a transcript, a phone number or a secret."""

    kind: str
    timestamp_ns: int
    data: str = None

    def __post_init__(self):
        if self.kind not in EVENT_KINDS:
            raise ValueError("unknown event kind: %r" % (self.kind,))
        if not _is_int(self.timestamp_ns) or self.timestamp_ns < 0:
            raise ValueError("timestamp_ns must be a non-negative integer")
        if self.kind == "dtmf":
            if not isinstance(self.data, str) or len(self.data) != 1 or self.data not in DTMF_DIGITS:
                raise ValueError("a dtmf event needs one digit from 0-9, *, # or A-D")
        elif self.data is not None and not isinstance(self.data, str):
            raise ValueError("data must be a string or None")


class Call(abc.ABC):
    """One phone call. The id is made on our side, so a remote party cannot choose it."""

    def __init__(self, direction):
        if direction not in DIRECTIONS:
            raise ValueError("direction must be 'inbound' or 'outbound'")
        self.direction = direction
        self.id = new_call_id()

    @abc.abstractmethod
    def audio_in(self):
        """An async iterator of Frame objects from the caller."""

    @abc.abstractmethod
    async def send_audio(self, frame):
        """Queue one Frame for playback to the caller."""

    @abc.abstractmethod
    async def clear_playback(self):
        """Barge-in: drop queued audio at once."""

    @abc.abstractmethod
    async def hangup(self):
        """End the call."""

    @property
    @abc.abstractmethod
    def events(self):
        """An async iterator of Event objects."""


class Adapter(abc.ABC):
    """A provider adapter. It must authenticate each call source before audio is processed (phase0-interface.md, section 4)."""

    @property
    @abc.abstractmethod
    def name(self):
        """A short adapter name."""

    @property
    @abc.abstractmethod
    def capabilities(self):
        """A Capabilities object."""

    @abc.abstractmethod
    async def run(self, on_call):
        """Accept calls and await on_call(call) for each one."""

    @abc.abstractmethod
    async def close(self):
        """Stop accepting calls and release resources."""
