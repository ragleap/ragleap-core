# Audio helpers for the core without `audioop` (draft)

Status: draft for discussion. Nothing here is implemented; every item is **planned**. Evidence tags: [verified] seen in code or documentation; [plan]; [unverified].

## 1. Problem

- [verified] `audioop` was deprecated in Python 3.11 and removed in 3.13; the last Python that has it is 3.12 (docs.python.org, PEP 594). ragleap-voice supports Python 3.10 and newer, and Colab's default Python is 3.13.
- [verified] The older voice channel in this repository (`channels/voice/router.py`) imports `audioop` for mu-law decoding and RMS. It cannot run on Python 3.13 without a replacement. A third-party port, `audioop-lts`, exists (seen in another project's pull request; not checked here).
- [verified] The core must stay standard library only (project rule), and `wave` remains available on 3.13 (the benchmark harness reads WAV with it on Colab).
- [plan] The ragleap-phone contract (`packages/ragleap-phone/docs/design/phase0-interface.md`) needs 20 ms frames, mu-law and A-law at the edge, 8 kHz to 16 kHz and back, and a level measure. The API design (section 13) needs a level check and a documented resampling method.

## 2. What the core needs

| Need | Detail |
|---|---|
| Decode and encode mu-law and A-law (G.711) | 8-bit codes to 16-bit linear PCM and back. A 256-entry table decodes; encoding can use a table over the top 14 bits. |
| Resample 8 kHz to 16 kHz and 16 kHz to 8 kHz | A method with a stated filter, because the resampler is part of the benchmark setup, not hidden inside an engine. |
| RMS and peak level | In dBFS, per frame and per clip, for the level check. |
| Gain to a target level | The noise pilot scaled every clip to -23 dBFS RMS with a 0.95 peak limit; the helper should do the same job in one documented place. |
| Frame slicing | Fixed 20 ms frames (160 samples at 8 kHz, 320 at 16 kHz), with a remainder policy. |
| Read and write WAV | `wave` for PCM 16-bit; other formats stay out of the core (engine extras may need ffmpeg). |

## 3. How, with the standard library only [plan]

- Samples as `array('h')` (signed 16-bit). Byte order is converted with `array.byteswap()` where needed. No third-party package is imported.
- Mu-law and A-law with precomputed tables built once at import.
- Resampling by an integer factor of 2: a short, fixed FIR low-pass filter for 16 kHz to 8 kHz; for 8 kHz to 16 kHz, zero-insertion followed by the same filter. Coefficients are written down in the module with their design (window, length, cutoff). Non-integer rates are out of scope for the core; an engine extra may use better tools.
- Level in a single pass over the array. Gain with clipping.

## 4. What is not known [unverified], and how to find out

1. **Speed.** Pure-Python loops over samples are slow. The budget is 20 ms of audio per frame per call. On the project's 1 vCPU server, how many concurrent calls does it sustain? Not measured. Proposal: a benchmark in `benchmarks/voice/` that times each helper on 20 ms frames under Python 3.10, 3.12 and 3.13, on the VPS (small, read-only work) and on Colab, and reports microseconds per frame and the implied number of simultaneous calls.
2. **Quality.** Does the FIR resampler change recognition accuracy compared with ffmpeg's resampler? The noise pilot's telephone path used ffmpeg. Proposal: repeat a small slice of that test with the helper's output in place of ffmpeg's and report the paired difference with an interval.
3. **Exactness.** Mu-law decoding is table-exact (G.711 defines it); the encoder and the A-law paths should be tested against known vectors and against ffmpeg's output on the same input.

## 5. If pure Python is too slow [plan]

- An optional extra may use a native or vectorised library for the same functions, behind the same API, and must be selected explicitly. The core never imports it.
- For ragleap-phone, per-frame work may belong in the call-handling process, not in ragleap-voice. That is a question for the interface contract, not for this note.

## 6. Open questions

1. Is a pure-Python resampler acceptable as the default if it is slower than real time on many concurrent calls, or does ragleap-voice document an extra as the recommended path for phone use?
2. Should the core normalise level itself (as the pilot did), or only report level and leave the decision to the caller (API design section 13, item 2)?
3. A-law: needed in 0.1.0, or only mu-law (what the Twilio channel uses) at first?
4. The older voice channel: someone owns the fix for Python 3.13. Whoever it is can reuse these helpers once they exist, or install the third-party port in the meantime.

## 7. First measurement (2026-10-22)

[verified] A prototype in `benchmarks/voice/audio_helpers_speed.py` (results in `benchmarks/voice/README.md`) measured pure-Python helpers on 20 ms frames on the project VPS (Python 3.10.12, 1 CPU, quiet): one call in both directions used about 6.4% of one core, so at most about 15 simultaneous calls if nothing else ran. The 8 to 16 kHz resampler is the largest part. Pure Python was roughly 9 to 215 times slower than `audioop` on the individual helpers.

This answers open question 1 for a pilot: a pure-Python default is workable for a few simultaneous calls. It does not show that it is acceptable at scale; an optional faster path (an extra) remains the plan for that. Not measured: Python 3.12 and 3.13, other CPUs, a polyphase resampler, resampling quality and its effect on recognition accuracy. The mu-law encoder in the prototype differs from `audioop` on 0.6% of input samples (381 of 65536, all negative inputs; in each case the prototype's code decodes closer to the original sample). Both are legitimate readings of G.711 section 3.6, which leaves the scaling and decision-value ties open; see "G.711 conventions (checked against the standard)" below.

## G.711 conventions (checked against the standard)

Source: ITU-T G.711 (1988), section 3.6 (page 1 of the extract), Tables 1a/1b (printed pages 3 and 4), Tables 2a/2b (printed pages 5 and 6). [verified: read from the PDF]

- Section 3.6 says the tables use a 13 (14) bit uniform code and that conversion at decision values is left to the individual equipment specification. It fixes neither the 16-bit scaling nor the tie rule.
- Section 3.2 zero code suppression (the code 00000010) is not implemented by `ulaw_encode` or `alaw_encode`. The tie labels in the table-check scripts mean: an exact decision value goes to the interval of larger magnitude (`tie-to-upper`) or the smaller one (`tie-to-lower`). No conformance claim is made.
- Decoders: mu-law and A-law both match the tables (x4 and x8) on all 256 codes, for ragleap and for Python audioop.
- Encoders: ragleap compares the exact magnitude with 4 * (mu-law) or 8 * (A-law) times the decision value; an input exactly on a decision value goes to the interval of larger magnitude. audioop matches the tables under a different scaling (floor for mu-law, one's-complement magnitude for A-law). The two differ only at negative decision values, always by one code (381 inputs for mu-law, 127 for A-law).
- Scripts: `benchmarks/voice/g711_mulaw_table_check.py` and `g711_alaw_table_check.py` test every convention and pick none.
- This is a stated convention, not a conformance claim. [unverified] Other published reference vectors (for example the G.191 tools, whose licence is not cleared) have not been used.
