"""
Voice channel adapter for RagLeap Core.

Handles Twilio Media Streams via WebSocket: receives real-time mu-law
8kHz audio, buffers it using voice-activity detection (VAD), transcribes
completed utterances through the configured STT provider (Whisper by
default, Deepgram when selected), answers via core.chat.ask(), and
streams the reply back as headerless 8 kHz mono G.711 mu-law TTS audio.

Single-tenant: one bot, one document set, configured entirely via .env
(see channels/voice/config.py). VAD tuning values below are carried over
from a production system where they were tuned against real call traffic.
"""
import asyncio
import audioop
import base64
import io
import json
import logging
import wave

from core.chat import ask
from core.employees.feedback import record_last_reply, get_last_reply, detect_feedback_command
from core.employees.learning import record_role_memory_outcome
from core.employees.channel_roles import resolve_role
from core.employees.tools import TOOL_REGISTRY
from channels.voice import audio as voice_audio
from channels.voice.config import get_voice_config

logger = logging.getLogger(__name__)

# Voice-activity detection tuning (carried over from production, tuned
# against real call traffic — change with caution).
MIN_SPEECH_FRAMES = 20      # 160ms minimum speech before transcribing
SILENCE_END_FRAMES = 20     # 400ms silence after speech = end of utterance
MAX_SPEECH_FRAMES = 240     # 5s hard cap — transcribe even without silence
MIN_RMS_FOR_SPEECH = 150    # RMS threshold for detecting speech vs. silence
ECHO_TAIL_FRAMES = 50       # 1s suppress after AI finishes speaking (echo tail)


async def send_audio(ws, stream_sid: str, audio_bytes: bytes):
    """Send headerless 8 kHz mono G.711 mu-law via Twilio's Media Streams protocol."""
    payload = base64.b64encode(audio_bytes).decode("utf-8")
    message = {
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload},
    }
    await ws.send(json.dumps(message))


async def send_mark(ws, stream_sid: str, name: str = "reply-done"):
    """
    Send a Twilio 'mark' event after streaming a reply — Twilio echoes this
    back once playback finishes, which we use to know when to resume
    listening for the caller's next turn.
    """
    message = {
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": name},
    }
    await ws.send(json.dumps(message))


async def speak_reply(ws, stream_sid: str, text: str, cfg: dict):
    """Stream headerless 8 kHz mono mu-law from the configured TTS provider, then mark completion."""
    loop = asyncio.get_running_loop()
    if cfg["tts_provider"] == "elevenlabs":
        tts_fn = voice_audio.text_to_speech_elevenlabs
    else:
        tts_fn = voice_audio.text_to_speech
    audio_bytes = await loop.run_in_executor(None, tts_fn, text, cfg["tts_api_key"], cfg["tts_voice"])
    if audio_bytes:
        await send_audio(ws, stream_sid, audio_bytes)
        await send_mark(ws, stream_sid)
        logger.info(f"Voice: streamed reply ({len(audio_bytes)} bytes)")
    else:
        logger.error("Voice: TTS returned no audio, skipping playback")


def _mulaw_to_wav(mulaw: bytes) -> bytes:
    """
    Wrap a raw G.711 mu-law utterance in a mono 16-bit 8 kHz WAV container.

    Twilio Media Streams captures 8 kHz mu-law; the STT providers are handed
    the resulting WAV bytes so each request carries what its audio/wav
    declaration claims.
    """
    pcm = audioop.ulaw2lin(mulaw, 2)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(pcm)
    return out.getvalue()


async def handle_call(ws):
    """
    Handle a Twilio Media Stream WebSocket connection: greet the caller,
    buffer their speech using VAD, transcribe completed utterances,
    answer via the core RAG pipeline, and speak the reply back.
    """
    stream_sid = None
    cfg = get_voice_config()
    loop = asyncio.get_running_loop()

    if cfg["stt_provider"] == "deepgram":
        stt_fn = voice_audio.transcribe_with_deepgram
    else:
        stt_fn = voice_audio.transcribe_audio

    collecting_buffer = bytearray()
    processing = False
    frame_count = 0
    ai_speaking = False
    echo_suppress_frames = 0

    consecutive_silence = 0
    total_speech_frames = 0
    has_speech_in_buffer = False

    try:
        async for msg in ws:
            d = json.loads(msg)
            ev = d.get("event", "")

            if ev == "connected":
                logger.info("Voice: Twilio connected")

            elif ev == "start":
                stream_sid = d.get("streamSid") or d.get("start", {}).get("streamSid", "")
                logger.info(f"Voice: call started, bot={cfg['bot_name']}")

                await asyncio.sleep(0.3)
                greeting = cfg["custom_greeting"].strip() or f"Hello! Welcome to {cfg['bot_name']}. How can I help you today?"
                await speak_reply(ws, stream_sid, greeting, cfg)
                ai_speaking = False

            elif ev == "media":
                chunk = base64.b64decode(d["media"]["payload"])
                frame_count += 1

                if ai_speaking:
                    continue
                if echo_suppress_frames > 0:
                    echo_suppress_frames -= 1
                    continue

                collecting_buffer.extend(chunk)

                try:
                    pcm_frame = audioop.ulaw2lin(bytes(chunk), 2)
                    frame_rms = audioop.rms(pcm_frame, 2)
                except Exception:
                    frame_rms = 0

                if frame_rms >= MIN_RMS_FOR_SPEECH:
                    total_speech_frames += 1
                    consecutive_silence = 0
                    has_speech_in_buffer = True
                else:
                    consecutive_silence += 1

                buffer_frames = len(collecting_buffer) // 160
                end_of_speech = (
                    has_speech_in_buffer
                    and total_speech_frames >= MIN_SPEECH_FRAMES
                    and consecutive_silence >= SILENCE_END_FRAMES
                    and not processing
                )
                hard_cap = (
                    has_speech_in_buffer
                    and buffer_frames >= MAX_SPEECH_FRAMES
                    and not processing
                )

                if end_of_speech or hard_cap:
                    speech_data = _mulaw_to_wav(bytes(collecting_buffer))
                    collecting_buffer.clear()
                    total_speech_frames = 0
                    consecutive_silence = 0
                    has_speech_in_buffer = False
                    processing = True
                    ai_speaking = True

                    logger.info(f"Voice: transcribing {len(speech_data)} bytes...")
                    try:
                        transcript = await asyncio.wait_for(
                            loop.run_in_executor(None, stt_fn, speech_data, cfg["stt_api_key"]),
                            timeout=15.0,
                        )
                    except asyncio.TimeoutError:
                        logger.error("Voice: STT timeout")
                        transcript = ""

                    if transcript and len(transcript) > 2:
                        feedback = detect_feedback_command(transcript)
                        if feedback is not None:
                            last_ids = get_last_reply("voice", stream_sid)
                            if last_ids:
                                record_role_memory_outcome(last_ids, success=feedback)
                            answer = "Thanks for the feedback!" if feedback else "Thanks, I'll do better next time."
                        else:
                            escalation = TOOL_REGISTRY["escalate_to_owner"].handler(
                                channel="voice", target=stream_sid, message_text=transcript
                            )
                            if escalation and escalation.get("status") in ("executed", "pending_approval"):
                                answer = "I've flagged this for a team member -- they'll follow up with you soon."
                            else:
                                try:
                                    result = ask(transcript, role=resolve_role("voice", transcript))
                                    answer = result.get("answer", "Sorry, I couldn't generate an answer.")
                                    record_last_reply("voice", stream_sid, result.get("role_memory_ids", []))
                                except Exception as e:
                                    logger.error(f"Voice: error answering: {e}", exc_info=True)
                                    answer = "Sorry, something went wrong. Please try again."

                        logger.info(f"Voice: answer: {answer[:120]}")
                        await speak_reply(ws, stream_sid, answer, cfg)

                    ai_speaking = False
                    processing = False

            elif ev == "mark":
                logger.info("Voice: reply finished, resuming listening")
                ai_speaking = False
                echo_suppress_frames = ECHO_TAIL_FRAMES
                collecting_buffer.clear()
                total_speech_frames = 0
                consecutive_silence = 0
                has_speech_in_buffer = False

            elif ev == "stop":
                logger.info("Voice: call ended")
                break

    except Exception as e:
        logger.error(f"Voice: error in handle_call: {e}", exc_info=True)
