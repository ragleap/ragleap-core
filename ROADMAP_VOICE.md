# RagLeap Voice Roadmap (Pre-Alpha)

`ragleap-voice` is an open, local speech stack aiming for STT/TTS, emotion control, and consent-based voice cloning without paid API dependencies in the default path.

## Current Status
- **Released (PyPI 0.0.2):** Model manager with manifest verification.
- **In Development (main):** API types, engine registry, and `transcribe()` with `faster-whisper` support.
- **Pending:** `synthesize()`, streaming, voice-activity gate, resampling, and voice cloning.

## How to Help
We are looking for contributors to tackle the following areas:

| Area | Issue |
|---|---|
| Whisper engine testing | #960 |
| Indic languages STT | #753 |
| CPU benchmarks | #755 |
| Listening-panel protocol | #756 |
| Faster Indic Parler-TTS | #757 |
| Open models (various) | #758, #759 |
| Hinglish test set | #760 |
| Speaker-similarity metric | #761 |
| API RFCs | #762 |
| Consent store | #763 |
| Model registry | #764 |
| Fine-tuning recipes | #765 |
| WER normalizer | #801 |
| Non-Twilio calling | #814 |

## Ground Rules
1. **Licences:** Only allowlisted licences (MIT, Apache-2.0, BSD, ISC, CC-BY-4.0) in the default path.
2. **Data:** Public datasets only. No real-person recordings or model weights in git.
3. **Voices:** Cloning requires explicit speaker consent.
4. **Hardware:** Use Colab or local machines; avoid shared servers.
5. **Coordination:** Comment on the relevant issue before starting work to avoid duplication.

See [CONTRIBUTING.md](CONTRIBUTING.md) for general project guidelines.