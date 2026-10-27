# Voice-activity gate and input level (draft)

Status: draft for review. Nothing here is implemented; every item is **planned**. Evidence tags: [verified] seen in code, docs or a run; [plan]; [unverified].
Any threshold below is a starting point from existing evidence, not a measured result.

## 1. Problem

- [verified] In the noise pilot, Whisper large-v3 wrote "Thank you." for silent audio (api.md section 13, item 1). The Whisper engine runs with `vad_filter=False` and says so in every result.
- [verified] More than half of the English FLEURS clips in the noise pilot were recorded very quietly (api.md section 13, item 2). An absolute loudness threshold would drop valid speech.
- [verified] The older voice channel uses an RMS threshold of 150 on 16-bit samples (about -46.8 dBFS), 20 ms frames, 20 frames of speech minimum and 20 frames of silence to end an utterance. Its code comment says these were tuned on production calls; that is [unverified] here, and it applies to 8 kHz Twilio audio only.
- [verified] The Silero licence was not verified. No model-based gate is allowed until it is checked at the source.
- [verified] No audio helper code exists in the package yet (audio-helpers.md is a design); the helpers must come first.

## 2. Principles

1. The gate never fails silently. Every drop or pass decision is reported in `notes` and in a returned decision record.
2. Configurable, and off must be possible.
3. Standard library only in the core. A model-based gate is an optional extra, blocked until its licence is verified.
4. The gate decides on level relative to the clip, not only on an absolute number, because quiet recordings exist.
5. No threshold is declared good until a benchmark under `benchmarks/voice/` measures false drops and hallucinations with intervals.

## 3. Two uses [plan]

**A. Batch (`transcribe()`).** Before the engine runs, compute per-frame RMS over 20 ms frames. If no frame is speech-like, return a `Transcript` with empty text and a note that says the clip was judged non-speech, with the level numbers, and do not call the engine. If some are, pass the whole clip through unchanged and add a note with the speech fraction. Cutting audio inside a clip is not part of the first version.

**B. Streaming (Stage 3).** Frame-by-frame state machine with a hangover (speech start, minimum speech length, silence to end an utterance, hard cap). The old channel's constants are the starting values to measure against, not defaults to trust. Out of scope until `transcribe_stream` exists.

## 4. Interface [plan]

- `transcribe(..., gate="energy" | "off")`. The default is an open question (section 7).
- `GateDecision` (frozen dataclass): `passed` (bool), `reason` (str), `speech_frames`, `total_frames`, `peak_dbfs`, `rms_dbfs`, `threshold_dbfs`.
- The decision text always appears in `Transcript.notes` when audio was dropped or when the level is far below a speech range (api.md section 13, items 1 and 2).
- Empty or known-filler output on low-energy audio gets a `notes` entry (item 6). The filler list is data in the repository, with its source named.

## 5. Order of work

1. Audio helpers PR (standard library): 20 ms framing with a stated remainder policy, RMS and peak in dBFS, mu-law and A-law checked against published G.711 test vectors, and the 2:1 resamplers with written-down filter coefficients. This also settles which mu-law encoder matches the reference.
2. Energy gate PR: `GateDecision`, the `gate` parameter, tests on synthetic silence, tones and noise.
3. Benchmark under `benchmarks/voice/` (Colab, public clips only): false-drop rate on speech clips including the quiet FLEURS ones, hallucination rate on silence and noise clips, each with an interval, for a few threshold rules.
4. Only then: streaming gate, and whether a model-based gate (after a licence check at the source) is worth adding.

## 6. Not decided here

- Whether the core normalises level or only reports it (api.md section 13, item 2; audio-helpers.md open question 2).
- Python 3.13: the helpers must not use `audioop`.
- Pure-Python speed: 6.4% of one core per call in both directions on the VPS (audio-helpers.md section 7), so a few simultaneous calls; more needs an optional faster extra.

## 7. Open questions

1. Default for `gate`: "energy" (protects against invented text, may drop quiet speech if the rule is wrong) or "off" (no behaviour change, no protection). The silence finding argues for "energy", but only after step 3 shows the false-drop rate.
2. Threshold rule: absolute dBFS, relative to the clip's own peak or median speech level, or a mix. The old channel's -46.8 dBFS is one data point for 8 kHz calls only.
3. Should a dropped clip raise an error or return empty text with a note? This draft returns empty text with a note, because a quiet room is a normal input, not a failure.
