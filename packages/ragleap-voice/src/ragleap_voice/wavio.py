"""WAV input for transcribe() (docs/design/api.md section 2). Standard library only.

The core reads PCM WAV with the standard wave module. Other formats need a decoder that an engine extra may bring.
"""
import io
import pathlib
import wave
from collections import namedtuple

from .errors import AudioDecodeError

MAX_BYTES = 1024 ** 3
WavInfo = namedtuple("WavInfo", "sample_rate channels sample_width frames duration_s")
_HINT = "give a PCM WAV file (a path or bytes); other formats need a decoder from an engine extra, or convert the file first."


def read_wav(audio):
    """Return (wav_bytes, WavInfo). Raises AudioDecodeError for anything that is not a non-empty PCM WAV."""
    if isinstance(audio, (bytes, bytearray)):
        data = bytes(audio)
    elif isinstance(audio, (str, pathlib.PurePath)):
        p = pathlib.Path(audio)
        try:
            size = p.stat().st_size
            if size > MAX_BYTES:
                raise AudioDecodeError("audio file %s is larger than %d bytes." % (p.name, MAX_BYTES), hint=_HINT)
            data = p.read_bytes()
        except OSError as exc:
            raise AudioDecodeError("cannot read audio file %s: %s." % (p.name, exc.strerror or exc), hint=_HINT)
    else:
        raise TypeError("audio must be a path or WAV bytes, got %s" % type(audio).__name__)
    if not data:
        raise AudioDecodeError("the audio is empty.", hint=_HINT)
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate, channels, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
    except (wave.Error, EOFError) as exc:
        raise AudioDecodeError("not a readable PCM WAV file (%s)." % exc, hint=_HINT)
    if frames == 0 or rate <= 0:
        raise AudioDecodeError("the WAV file contains no audio frames.", hint=_HINT)
    return data, WavInfo(rate, channels, width, frames, frames / float(rate))
