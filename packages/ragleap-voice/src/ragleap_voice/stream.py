"""transcribe_stream(): a synchronous stream over the batch transcribe() (docs/design/streaming.md). Standard library only.

Status: pre-alpha; transcribe_stream is exported from the package. This is a batch adapter, NOT incremental recognition: audio is buffered, StreamGate decides
whether any speech qualified, and the existing engine runs once at finish() on all the audio. No partial hypotheses, no
low-latency claim. Cancellation stops event delivery; it does not interrupt a model call already running.
"""
import array
import io
import sys
import time
import uuid
import wave
from dataclasses import dataclass, field

from .errors import AudioDecodeError, RagleapVoiceError, UnsupportedFeatureError
from .gate import StreamGate

DEFAULT_MAX_BYTES = 16000 * 2 * 600  # 10 minutes of 16 kHz mono PCM16; an unmeasured starting point


@dataclass(frozen=True)
class StreamEvent:
    stream_id: str
    sequence: int
    type: str
    timestamp: float
    data: dict = field(default_factory=dict)


def _wav(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    return buf.getvalue()


class TranscriptStream:
    """feed(bytes), finish(), cancel(), and iterate events. Input is mono signed 16-bit little-endian PCM at `rate`."""

    def __init__(self, rate=16000, *, chunks=None, gate="energy", gate_options=None, max_bytes=DEFAULT_MAX_BYTES,
                 runner):
        if isinstance(rate, bool) or not isinstance(rate, int) or rate < 1:
            raise ValueError("rate must be a positive integer")
        if gate not in ("off", "energy"):
            raise ValueError("gate must be 'off' or 'energy'")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 2:
            raise ValueError("max_bytes must be a positive integer")
        self.stream_id = uuid.uuid4().hex[:12]
        self._rate, self._max, self._runner, self._source = rate, max_bytes, runner, chunks
        self._gate = StreamGate(rate, gate_options) if gate == "energy" else None
        self._pcm, self._carry = bytearray(), b""
        self._speech = False
        self._finished = self._cancelled = False
        self._seq = 0
        self._it = None

    def _check_open(self):
        if self._finished or self._cancelled:
            raise RagleapVoiceError("the stream no longer accepts audio (finished or cancelled).")

    def feed(self, data):
        """Submit the next block. Rejects the block, changing nothing, if it would exceed max_bytes."""
        self._check_open()
        data = bytes(data)
        if len(self._pcm) + len(self._carry) + len(data) > self._max:
            raise RagleapVoiceError("input limit of %d bytes reached." % self._max,
                                    hint="finish() the stream, or raise max_bytes.")
        buf = self._carry + data
        n = len(buf) // 2 * 2
        self._carry, block = buf[n:], buf[:n]
        self._pcm += block
        if self._gate is not None and block:
            s = array.array("h")
            s.frombytes(block)
            if sys.byteorder == "big":
                s.byteswap()
            self._note(self._gate.feed(s))

    def _note(self, events):
        if any(e.kind == "speech_start" for e in events):
            self._speech = True

    def finish(self):
        """Close the input. Idempotent."""
        if self._finished or self._cancelled:
            return
        self._finished = True
        if self._gate is not None:
            self._note(self._gate.finish())

    def cancel(self):
        """Stop delivery. Idempotent. Iteration then ends with one stream.cancelled event."""
        self._cancelled = True

    def _ev(self, type_, data):
        e = StreamEvent(self.stream_id, self._seq, type_, time.monotonic(), data)
        self._seq += 1
        return e

    def _error(self, exc):
        return self._ev("stream.error", {"error_type": type(exc).__name__, "message": str(exc)[:300],
                                         "hint": getattr(exc, "hint", None)})

    def __iter__(self):
        if self._it is None:
            if self._source is None and not self._finished and not self._cancelled:
                raise RagleapVoiceError("call finish() before iterating, or pass chunks=.")
            self._it = self._events()
        return self._it

    def _events(self):
        yield self._ev("stream.started", {"operation": "transcribe", "rate": self._rate})
        try:
            if self._source is not None:
                for c in self._source:
                    if self._cancelled:
                        break
                    self.feed(c)
                self.finish()
        except Exception as exc:
            yield self._error(exc)
            return
        if self._cancelled:
            yield self._ev("stream.cancelled", {})
            return
        if self._carry:
            yield self._error(AudioDecodeError("the input ended in the middle of a 16-bit sample."))
            return
        if not self._pcm:
            yield self._ev("stream.no_speech", {"reason": "no audio was received"})
            return
        if self._gate is not None and not self._speech:
            yield self._ev("stream.no_speech", {"reason": "no utterance reached the speech onset rule; the engine was not run"})
            return
        try:
            tr = self._runner(_wav(self._pcm, self._rate))
        except Exception as exc:
            yield self._error(exc)
            return
        if self._cancelled:
            yield self._ev("stream.cancelled", {})
            return
        for seg in tr.segments:
            yield self._ev("transcript.segment", {"start": seg.start, "end": seg.end, "text": seg.text})
        yield self._ev("transcript.final", {"text": tr.text, "language": tr.language, "engine": tr.engine,
                                            "model": tr.model, "model_status": tr.model_status, "notes": list(tr.notes)})


def transcribe_stream(chunks=None, *, rate=16000, language=None, engine="auto", allow_unverified=False, cache_dir=None,
                      options=None, gate="energy", gate_options=None, max_bytes=DEFAULT_MAX_BYTES):
    """Return a TranscriptStream. Pass chunks (an iterable of PCM16 bytes) or call feed() and finish() yourself."""
    from .api import transcribe

    def runner(wav):
        return transcribe(wav, language=language, engine=engine, allow_unverified=allow_unverified, cache_dir=cache_dir,
                          timestamps=True, options=options, gate="off")

    return TranscriptStream(rate, chunks=chunks, gate=gate, gate_options=gate_options, max_bytes=max_bytes, runner=runner)
