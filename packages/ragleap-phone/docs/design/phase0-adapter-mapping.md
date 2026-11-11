# ragleap-phone, Phase 0: mapping two backends onto the adapter contract (draft)

Status: design only, nothing built or run. Tags: [read] seen in docs or code on 2026-11-10 or 2026-11-11, [plan] intention, [unverified] not checked. Companion to phase0-interface.md (sections 3 to 6).

## 1. A gap in the audio contract

Section 4 says the phone side is 8 kHz and the adapter decodes the codec at its edge. Neither backend below hands us 8 kHz G.711. Proposal [plan]: a frame declares its own sample rate; each adapter states in `capabilities` the rate it delivers; one shared helper in ragleap-phone converts to the 16 kHz that ragleap-voice expects, so adapters do not each carry resampling code.

## 2. jambonz

- audio_in: the `listen` verb streams call audio over a WebSocket to a server we run, as L16 PCM binary frames; the rate is set in the verb (8000, 16000, 24000, 48000 or 64000). Asking for 16000 needs no `Up2Stream`. [read, docs.jambonz.org]
- send_audio: bidirectional streaming takes raw L16 binary frames at a separately set rate; not supported when `listen` is nested in `dial`, so bridging a caller to another party needs a different path. [read]
- dtmf: optional JSON text frames, which map to the `dtmf` event. [read]
- clear_playback, playback.finished (the equivalent of Twilio's mark): not found in the pages read. [unverified]
- Authentication: webhooks and the media WebSocket are separate connections and each must be authenticated under its own contract (section 4 of phase0-interface.md). Signature or token details. [unverified]
- Licence: code MIT in the repositories checked; the packaged product needs a key (see the table in section 6). [read]

## 3. LiveKit (SIP service and an agent in the room)

- Inbound: the SIP service decodes G.711 and Opus itself and publishes into the room at a room rate of 48000 (livekit/sip, pkg/sip/room.go and media.go). [read, not run]
- Gap: our helpers convert by a factor of 2. Going from 48000 to 16000 is a factor of 3. Options [plan]: (a) ask the agent side for a lower rate, if it offers one [unverified]; (b) add a small 3:1 decimator with an anti-alias filter to ragleap-phone, tested with the same method as `Down2Stream`.
- Outbound: audio is published to the room as an Opus track at the room rate, so 16 kHz synthesis output needs a 1:3 upsample. [read, not run]
- dtmf, barge-in, how an agent receives frames, latency, token format: not read. [unverified]

## 4. Plain SIP servers (unchanged)

The adapter would decode G.711 with `ulaw_decode` or `alaw_decode` and then `Up2Stream`. Section 3.2 zero-code suppression is not implemented in those helpers; no conformance claim.

## 5. Independence

Each backend is an optional adapter in its own extra, swappable, never imported by the core. [plan] jambonz, LiveKit and Twilio are all replaceable by a plain SIP adapter; nothing in ragleap-voice depends on any of them.

## 6. To test before any claim

Frame size and jitter on each path, end-to-end latency, behaviour at 8 kHz and 16 kHz, authentication per transport, and whether the open jambonz 0.9.x line runs without the licence key. No number is promised here.
