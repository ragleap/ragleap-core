"""synthesize_stream(): chunked playback of a finished waveform (docs/design/streaming.md). Standard library only.

Status: pre-alpha, not exported. The engine synthesizes the whole utterance first (existing synthesize()); this module then
splits the WAV into ordered chunks. It is NOT incremental model generation and gives no first-audio latency advantage.
Cancellation stops delivery between chunks; it cannot interrupt a synthesis call already running.
"""
import io
import time
import uuid
import wave

from .errors import RagleapVoiceError, UnsupportedFeatureError
from .stream import StreamEvent

DEFAULT_MAX_BYTES = 16000 * 2 * 600  # 10 minutes of 16 kHz mono PCM16; an unmeasured starting point


class SpeechStream:
    """Iterate events; call cancel() at any time. The waveform comes from `runner()`, which returns a Speech."""

    def __init__(self, runner, *, chunk_ms=20, max_bytes=DEFAULT_MAX_BYTES):
        if isinstance(chunk_ms, bool) or not isinstance(chunk_ms, int) or chunk_ms < 1:
            raise ValueError("chunk_ms must be a positive integer")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.stream_id = uuid.uuid4().hex[:12]
        self._runner, self._chunk_ms, self._max = runner, chunk_ms, max_bytes
        self._cancelled = False
        self._seq = 0
        self._it = None

    def cancel(self):
        """Stop delivery. Idempotent."""
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
            self._it = self._events()
        return self._it

    def _events(self):
        yield self._ev("stream.started", {"operation": "synthesize"})
        if self._cancelled:
            yield self._ev("stream.cancelled", {})
            return
        try:
            speech = self._runner()
            with wave.open(io.BytesIO(speech.wav), "rb") as w:
                channels, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
                if width != 2:
                    raise UnsupportedFeatureError("only 16-bit PCM WAV can be chunked, got %d-bit." % (width * 8))
                if n * channels * width > self._max:
                    raise RagleapVoiceError("the waveform is larger than max_bytes (%d)." % self._max,
                                            hint="raise max_bytes or synthesize shorter text.")
                pcm = w.readframes(n)
        except Exception as exc:
            yield self._error(exc)
            return
        step = max(1, rate * self._chunk_ms // 1000) * channels * width
        count = 0
        for i in range(0, len(pcm), step):
            if self._cancelled:
                yield self._ev("stream.cancelled", {})
                return
            yield self._ev("audio.chunk", {"index": count, "audio": pcm[i:i + step], "sample_rate": rate,
                                           "channels": channels, "sample_format": "s16le"})
            count += 1
        if self._cancelled:
            yield self._ev("stream.cancelled", {})
            return
        yield self._ev("audio.completed", {"chunks": count, "sample_rate": rate, "channels": channels,
                                           "duration_s": n / rate if rate else 0.0, "engine": speech.engine,
                                           "model": speech.model, "model_status": speech.model_status,
                                           "synthetic": True, "notes": list(speech.notes)})


def synthesize_stream(text, *, language, engine="auto", emotion=None, speed=None, allow_unverified=False, cache_dir=None,
                      options=None, chunk_ms=20, max_bytes=DEFAULT_MAX_BYTES):
    """Return a SpeechStream. Chunked playback of a finished waveform, not incremental generation."""
    from .synth import synthesize

    def runner():
        return synthesize(text, language=language, engine=engine, emotion=emotion, speed=speed,
                          allow_unverified=allow_unverified, cache_dir=cache_dir, options=options)

    return SpeechStream(runner, chunk_ms=chunk_ms, max_bytes=max_bytes)
