# Streaming (v1, implemented as adapters)

Status: pre-alpha. Evidence tags: [verified] merged on main and tested with fakes; [plan]; [unverified]. Drafted with a peer engineer's design; claims here follow the merged code.

## What exists (all in `ragleap_voice`, none exported yet)

- [verified] `audio.Down2Stream`, `audio.Up2Stream` (PR 1084): stateful 2:1 resamplers; chunked output equals the one-shot `down2`/`up2` exactly; `process()` after `finish()` raises `ValueError`.
- [verified] `gate.StreamGate` (PR 1085): frame-by-frame energy gate (speech_start, speech_end, no_speech). A level gate cannot tell loud constant noise from speech; thresholds are unmeasured starting points.
- [verified] `stream.transcribe_stream` / `TranscriptStream` (PR 1086): `feed`, `finish`, `cancel`, event iteration. A batch adapter: input is buffered in the parent (bounded by `max_bytes`), `StreamGate` decides whether speech qualified, and the existing `transcribe()` runs once at `finish()`. No partial transcripts; no latency claim. Lifecycle misuse raises `RagleapVoiceError`.
- [verified] `synth_stream.synthesize_stream` / `SpeechStream` (PR 1088): chunked playback of a finished waveform. The engine synthesizes everything first; this is not incremental generation.

## Events

Each event is a `StreamEvent(stream_id, sequence, type, timestamp, data)`. `sequence` starts at 0 and rises by one. Exactly one terminal event ends a stream.

| type | stream | data |
|---|---|---|
| `stream.started` | both | `operation` |
| `transcript.segment` | transcription | `start`, `end` (seconds), `text` |
| `transcript.final` | transcription, terminal | `text`, `language`, `engine`, `model`, `model_status`, `notes` |
| `stream.no_speech` | transcription, terminal | `reason` |
| `audio.chunk` | synthesis | `index`, `audio` (PCM16 bytes), `sample_rate`, `channels`, `sample_format` |
| `audio.completed` | synthesis, terminal | `chunks`, `sample_rate`, `channels`, `duration_s`, `synthetic`, `engine`, `model`, `model_status`, `notes` |
| `stream.cancelled` | both, terminal | none |
| `stream.error` | both, terminal | `error_type`, `message`, `hint` |

## Limits and honesty rules

- [verified] Cancellation stops delivery; it cannot interrupt a model call already running.
- [verified] Whisper stays a batch engine; `Capabilities.streaming` stays False. A wrapper does not change that.
- [plan] Report time to first committed segment, to final transcript and to first playable chunk, with rates, codec and whether the engine is a batch adapter. Never report a partial-result latency for a buffered batch adapter.
- [plan] Phone side: inbound 8 kHz to 16 kHz with `Up2Stream`; outbound, read the synthesis result's sample rate (never assume 16 kHz); only a rate twice 8 kHz uses `Down2Stream`.

## G.711 mu-law

- [verified] Decoder: identical to audioop for all 256 codes.
- [verified] Encoder vs audioop over all 65536 inputs: 381 mismatches, all negative inputs, all one code apart; ragleap's decoded value is closer to the input in 381 of 381; 0 lie on the midpoint of the two decoded values, 120 within 1 of it, 261 elsewhere.
- [unverified] No conformance claim: published reference vectors are still needed, and A-law is not implemented. G.711 section 3.6 has not been read by us.

## Not built

- [plan] A worker-side streaming protocol (separate no-id event messages, worker generations, bounded queues). It is not needed by the adapters above. Design: `worker-streaming.md` when added.
- [plan] Real-phone latency, resampling quality and noise behaviour are unmeasured.
