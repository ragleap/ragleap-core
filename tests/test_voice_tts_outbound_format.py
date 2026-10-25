"""TTS replies reach Twilio as headerless 8 kHz mono G.711 mu-law, offline."""
import asyncio
import base64
import json
import math
import struct
from unittest.mock import patch

import pytest
import requests

from channels.voice import audio as voice_audio
from channels.voice import router as voice_router
from test_voice_providers import FakeWS, _mu_law_reference


MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00" + bytes(20)
# Every G.711 code, with a non-silence prefix so it cannot resemble MPEG sync.
MULAW_BYTES = b"\x80\x00" + bytes(range(256))
SKIP_LINE = "Voice: TTS returned no audio, skipping playback"


def _pcm_samples(count):
    """Two changing tones break rate, endian and companding fixed points."""
    return [int(10000 * math.sin(2 * math.pi * 437 * i / 24000 + 0.3)
                + 4300 * math.sin(2 * math.pi * 931 * i / 24000 + 0.7))
            for i in range(count)]


class TtsResponse:
    """Only the response surface consumed by the real TTS functions."""

    def __init__(self, content, http_error=False):
        self.content = content
        self.http_error = http_error

    def raise_for_status(self):
        if self.http_error:
            raise requests.HTTPError("example-provider-error")


class FormatHonouringProvider:
    """Serve bytes by request metadata; fail closed on unmodelled requests."""

    def __init__(self, provider, pcm, outcome="audio"):
        self.provider = provider
        self.pcm = pcm
        self.outcome = outcome
        self.calls = []
        self.replies = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        expected_url = (
            "https://api.elevenlabs.io/v1/text-to-speech/example-voice-id"
            if self.provider == "elevenlabs"
            else "https://api.openai.com/v1/audio/speech"
        )
        if url != expected_url:
            pytest.fail(f"unexpected provider URL: {url!r}")
        if set(kwargs) - {"headers", "json", "params", "timeout"}:
            pytest.fail(f"unexpected request arguments: {set(kwargs)}")
        body = kwargs.get("json", {})
        params = kwargs.get("params", {})
        if self.provider == "openai":
            if params or set(body) != {"model", "input", "voice", "response_format"}:
                pytest.fail(f"unexpected OpenAI request metadata: {body!r}, {params!r}")
            declared = body.get("response_format")
            if declared not in ("mp3", "pcm"):
                pytest.fail(f"unmodelled OpenAI response_format: {declared!r}")
            content = self.pcm if declared == "pcm" else MP3_BYTES
        else:
            # A body output_format is deliberately ignored: this API uses the query.
            if (set(params) - {"output_format"}
                    or set(body) - {"text", "model_id", "output_format"}):
                pytest.fail(f"unexpected ElevenLabs request metadata: {body!r}, {params!r}")
            declared = params.get("output_format")
            if declared not in (None, "mp3_44100_128", "ulaw_8000"):
                pytest.fail(f"unmodelled ElevenLabs output_format: {declared!r}")
            content = MULAW_BYTES if declared == "ulaw_8000" else MP3_BYTES
        if self.outcome == "exception":
            raise requests.RequestException("example-provider-unavailable")
        if self.outcome == "empty":
            content = b""
        self.replies.append((declared, content))
        return TtsResponse(content, http_error=self.outcome == "http-error")


def _speak(provider, count=720, trailing=b"", outcome="audio"):
    """Only fake HTTP; keep dispatch, provider guards, conversion and socket real."""
    samples = _pcm_samples(count)
    fake = FormatHonouringProvider(
        provider, struct.pack(f"<{count}h", *samples) + trailing, outcome
    )
    cfg = {
        "tts_provider": provider,
        "tts_api_key": "" if outcome == "missing-key" else "example-tts-key",
        "tts_voice": "example-voice-id" if provider == "elevenlabs" else "nova",
    }
    ws = FakeWS([])
    with patch.object(voice_audio.requests, "post", side_effect=fake):
        asyncio.run(voice_router.speak_reply(ws, "example-stream-sid", "Hello!", cfg))
    return fake, [json.loads(message) for message in ws.sent]


@pytest.mark.parametrize("provider,count,trailing", [
    pytest.param("openai", 720, b"", id="openai-pcm"),
    # 721 samples -> 241 bytes; dropping one complete sample loses a grid point.
    pytest.param("openai", 721, b"", id="openai-remainder-one"),
    pytest.param("openai", 722, b"", id="openai-non-multiple-of-three"),
    pytest.param("openai", 722, b"\x91", id="openai-odd-length-pcm"),
    pytest.param("elevenlabs", 720, b"", id="elevenlabs-ulaw"),
])
def test_speak_reply_sends_headerless_mulaw_media_payload(provider, count, trailing):
    """Kill MP3, rate, endian, A-law, double conversion and partial-sample regressions."""
    fake, messages = _speak(provider, count, trailing)
    assert len(fake.calls) == 1, "request format: the truthy TTS guard must reach HTTP"
    media = [message for message in messages if message.get("event") == "media"]
    assert len(media) == 1, "request format: nonempty TTS must produce one media payload"
    payload = base64.b64decode(media[0]["media"]["payload"], validate=True)
    assert not payload.startswith((b"ID3", b"RIFF", b"OggS", b"fLaC")), (
        f"container header reached Twilio: {payload[:4]!r}; expected headerless 8 kHz mu-law"
    )
    assert not (len(payload) > 1 and payload[0] == 0xFF and payload[1] & 0xE0 == 0xE0), (
        "MPEG frame sync reached Twilio; expected headerless 8 kHz mu-law"
    )
    table = _mu_law_reference()
    decoded = [table[code] for code in payload]
    declared, response = fake.replies[0]
    if provider == "openai":
        assert declared == "pcm", "OpenAI response_format must declare pcm"
        # Decode the fake response by its declared format, discarding just a partial sample.
        complete = response[:len(response) // 2 * 2]
        source = struct.unpack(f"<{len(complete) // 2}h", complete)
        # At an integer 3:1 ratio the output grid lands on every third input sample.
        expected = source[::3]
        assert len(payload) == len(expected), (
            "OpenAI duration/resampling: preserve every complete sample at 24 kHz -> 8 kHz"
        )
        assert abs(len(payload) - count / 3) < 1, (
            "OpenAI duration: error must be less than one 8 kHz sample (125 microseconds)"
        )
        # Allow half a 512-unit G.711 bin plus four PCM units for integer rounding.
        assert max(abs(actual - wanted) for actual, wanted in zip(decoded, expected)) <= 260, (
            "OpenAI mu-law PCM fidelity: wrong rate, byte order or A-law exceeds 260 PCM units"
        )
    else:
        assert declared == "ulaw_8000", "ElevenLabs query output_format must declare ulaw_8000"
        assert len(payload) == len(response), "ElevenLabs duration: mu-law length must pass unchanged"
        assert payload == response, "ElevenLabs mu-law content must pass unchanged, without PCM conversion"
        assert decoded == [table[code] for code in response], "ElevenLabs G.711 reference mismatch"
    assert [message["event"] for message in messages] == ["media", "mark"]
    assert all(message["streamSid"] == "example-stream-sid" for message in messages)


@pytest.mark.parametrize("provider", ["openai", "elevenlabs"])
def test_speak_reply_requests_twilio_compatible_format(provider):
    """Kill MP3 requests and a dropped or body-only ElevenLabs output_format."""
    fake, _ = _speak(provider)
    assert len(fake.calls) == 1, "request format: the truthy TTS guard must reach HTTP"
    _, kwargs = fake.calls[0]
    if provider == "openai":
        assert kwargs.get("json", {}).get("response_format") == "pcm", (
            "OpenAI request format: response_format must be pcm in the JSON body"
        )
        assert not kwargs.get("params"), "OpenAI response_format belongs in JSON"
    else:
        assert kwargs.get("params", {}).get("output_format") == "ulaw_8000", (
            "ElevenLabs request format: query output_format must be ulaw_8000"
        )
        assert "output_format" not in kwargs.get("json", {}), (
            "ElevenLabs request format: output_format belongs in the query, not JSON"
        )


@pytest.mark.parametrize("provider", ["openai", "elevenlabs"])
@pytest.mark.parametrize("outcome", ["empty", "exception", "http-error", "missing-key"])
def test_speak_reply_skips_empty_or_failed_tts(provider, outcome, caplog):
    """Kill media or a completion mark on empty, failed or unconfigured TTS."""
    fake, messages = _speak(provider, outcome=outcome)
    assert len(fake.calls) == (0 if outcome == "missing-key" else 1), (
        "configured TTS must reach HTTP; missing-key TTS must stop at its guard"
    )
    assert not any(message.get("event") == "media" for message in messages), (
        "empty or failed TTS must send no media"
    )
    assert not any(message.get("event") == "mark" for message in messages), (
        "empty or failed TTS must send no mark"
    )
    assert messages == [], "empty or failed TTS must send no socket messages"
    assert SKIP_LINE in caplog.messages, "empty or failed TTS must log the existing skip line"
