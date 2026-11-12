# ragleap-phone (planned)

**Status: planned.** Only a package skeleton exists: a frame type, the adapter interfaces, a sample-rate converter, the G.711 audio path, RTP packet handling and a UDP media endpoint for one call, with unit tests. No SIP signalling, nothing tested against a real PBX or trunk, nothing released.

The goal: phone calls for RagLeap without Twilio, as independent of any single provider as possible. This package would handle calls, SIP and audio. Speech stays in `ragleap-voice`.

- Call for contributors, country by country: issue #814
- Plan, decisions and conventions: [docs/HANDOVER.md](docs/HANDOVER.md)

Emergency calling will never be supported.
