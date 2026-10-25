"""
Speech-to-text and text-to-speech for the RagLeap Core voice channel.
STT: OpenAI Whisper (default) or Deepgram. TTS: OpenAI TTS (default) or
ElevenLabs. The provider is selected per deployment via VOICE_STT_PROVIDER
and VOICE_TTS_PROVIDER (see config.py). Both TTS functions return
headerless 8 kHz mono G.711 mu-law for Twilio playback.
"""
import audioop
import io
import logging

import requests

logger = logging.getLogger(__name__)

OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
DEEPGRAM_STT_URL = "https://api.deepgram.com/v1/listen"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


def transcribe_audio(pcm_audio: bytes, api_key: str, sample_rate: int = 8000) -> str:
    """
    Transcribe a WAV utterance using OpenAI Whisper.
    `pcm_audio` is a complete mono 8 kHz 16-bit WAV file — the router
    converts Twilio's raw mu-law into this shape before calling.
    `sample_rate` is not sent: the WAV header already carries the rate;
    it exists only so both STT functions share one signature. Returns the
    transcript, or "" when no key is configured or the request/parse fails.
    """
    if not api_key:
        logger.error("No OpenAI API key configured for STT — set OPENAI_API_KEY in .env")
        return ""

    files = {"file": ("audio.wav", io.BytesIO(pcm_audio), "audio/wav")}
    data = {"model": "whisper-1"}
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        response = requests.post(OPENAI_STT_URL, headers=headers, files=files, data=data, timeout=15)
        response.raise_for_status()
        return response.json().get("text", "").strip()
    except Exception as e:
        logger.error(f"Whisper transcription failed: {e}")
        return ""


def text_to_speech(text: str, api_key: str, voice: str = "nova") -> bytes:
    """Generate OpenAI speech as headerless 8 kHz mono G.711 mu-law for Twilio."""
    if not api_key:
        logger.error("No OpenAI API key configured for TTS — set OPENAI_API_KEY in .env")
        return b""

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": "tts-1", "input": text, "voice": voice, "response_format": "pcm"}

    try:
        response = requests.post(OPENAI_TTS_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        # OpenAI PCM is headerless 24 kHz mono signed 16-bit little-endian; drop a partial sample.
        pcm = response.content[:len(response.content) // 2 * 2]
        pcm, _ = audioop.ratecv(pcm, 2, 1, 24000, 8000, None)
        return audioop.lin2ulaw(pcm, 2)
    except Exception as e:
        logger.error(f"OpenAI TTS failed: {e}")
        return b""


def transcribe_with_deepgram(pcm_audio: bytes, api_key: str, sample_rate: int = 8000) -> str:
    """
    Transcribe a WAV utterance using Deepgram (nova-2 with smart formatting).
    `pcm_audio` is the same mono 8 kHz 16-bit WAV file the Whisper path
    receives; `sample_rate` is not sent: the WAV header already carries the
    rate, it exists only so both STT functions share one signature. Returns
    the transcript, or "" when no key is configured or the request/parse
    fails.
    """
    if not api_key:
        logger.error("No Deepgram API key configured for STT — set DEEPGRAM_API_KEY in .env")
        return ""

    headers = {"Authorization": f"Token {api_key}", "Content-Type": "audio/wav"}
    params = {"model": "nova-2", "smart_format": "true"}

    try:
        response = requests.post(
            DEEPGRAM_STT_URL, headers=headers, params=params, data=pcm_audio, timeout=15
        )
        response.raise_for_status()
        transcript = response.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
        return (transcript or "").strip()
    except Exception as e:
        logger.error(f"Deepgram transcription failed: {e}")
        return ""


def text_to_speech_elevenlabs(text: str, api_key: str, voice: str) -> bytes:
    """Generate ElevenLabs speech as headerless 8 kHz mono G.711 mu-law for Twilio."""
    if not api_key:
        logger.error("No ElevenLabs API key configured for TTS — set ELEVENLABS_API_KEY in .env")
        return b""

    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {"text": text, "model_id": "eleven_multilingual_v2"}

    try:
        response = requests.post(
            ELEVENLABS_TTS_URL.format(voice_id=voice), headers=headers, json=payload,
            params={"output_format": "ulaw_8000"}, timeout=30
        )
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.error(f"ElevenLabs TTS failed: {e}")
        return b""
