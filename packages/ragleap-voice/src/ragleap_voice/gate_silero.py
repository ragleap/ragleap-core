"""Silero VAD as an opt-in gate of transcribe() (gate="silero"; docs/design/vad-gate.md sections 9 and 10).

Standard library only at import time: vad_silero is imported when a gate is built, and onnxruntime only when the model file is loaded.
The model file is supplied by the caller (gate_options={"model_path": ...}); it is not bundled and must have the pinned SHA-256.
"""
import array
import io
import os
import sys
import wave

from .errors import AudioDecodeError, UnsupportedFeatureError

RATE = 16000
_MEASURED = ("On 796 real recorded noise files (one run, no speech mixed in) it reported speech on 4.4% to 5.4% of them, "
             "against about 99% for the level gate (benchmarks/voice/README.md); its behaviour on speech was not measured.")


def validate_options(options):
    """Return (model_path, detector options) or raise. Called before any model work."""
    from . import vad_silero
    opts = dict(options or {})
    path = opts.pop("model_path", None)
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str) or not path.strip():
        raise UnsupportedFeatureError('gate="silero" needs gate_options={"model_path": "/path/to/silero_vad_16k_op15.onnx"}.',
                                      hint='the file is not bundled and must have the pinned SHA-256 (docs/design/vad-gate.md section 9); '
                                           'it needs the extra: pip install "ragleap-voice[vad]".')
    unknown = sorted(set(opts) - set(vad_silero.DEFAULTS))
    if unknown:
        raise UnsupportedFeatureError("unknown gate option(s): %s." % ", ".join(unknown),
                                      hint="supported: model_path, %s." % ", ".join(sorted(vad_silero.DEFAULTS)))
    return path, opts


def decide(wav_bytes, parsed):
    """Run the detector over a mono 16-bit 16 kHz WAV. Returns (passed, note)."""
    from . import vad_silero
    path, opts = parsed
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            channels, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if (channels, width, rate) != (1, 2, RATE):
                raise UnsupportedFeatureError(
                    "the silero gate needs mono 16-bit PCM WAV at %d Hz, got %d channel(s), %d-bit, %d Hz." % (RATE, channels, width * 8, rate),
                    hint='convert first, or pass gate="off".')
            raw = w.readframes(n)
    except (wave.Error, EOFError) as exc:
        raise AudioDecodeError("not a readable PCM WAV file (%s)." % exc)
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    gate = vad_silero.SileroGate(RATE, path, opts)
    events = gate.feed(samples) + gate.finish()
    starts = [e for e in events if e.kind == "speech_start"]
    thr = opts.get("threshold", vad_silero.DEFAULTS["threshold"])
    need = opts.get("min_speech_windows", vad_silero.DEFAULTS["min_speech_windows"])
    if starts:
        return True, ("silero gate: passed (speech from %.2f s, %d utterance(s) at threshold %s); the engine ran on the whole clip. %s"
                      % (starts[0].time_s, len(starts), thr, _MEASURED))
    return False, ("silero gate: no speech detected in %d windows (threshold %s, %d consecutive windows needed), the engine was not run. "
                   "Speech shorter than about %.1f s is dropped too. %s"
                   % (len(samples) // vad_silero.WINDOW, thr, need, need * vad_silero.WINDOW / RATE, _MEASURED))
