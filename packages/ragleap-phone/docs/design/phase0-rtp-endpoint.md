# ragleap-phone, Phase 0: RTP media endpoint (design, draft)

Status: design only, nothing built or run. Tags: [read] seen in a source on 2026-11-11, [plan] intention, [unverified] from memory or not checked. Every default below is a starting point, not a measurement. Companion to phase0-interface.md (sections 3 and 4) and phase0-adapter-mapping.md. The audio path this endpoint feeds already exists (`InboundAudio`, `OutboundAudio`).

## 1. Scope

The endpoint receives and sends the RTP media of one call as G.711 (mu-law or A-law) and hands it to the audio path. It does not do signalling: a SIP server (Asterisk, FreeSWITCH, Kamailio with rtpengine, or another) sets the call up and tells the operator which address and port to send media to and which to expect it from. It does not implement SDP, SRTP, RTCP, DTMF events or a jitter buffer (section 10). Standard library only (`asyncio` and `socket`); no vendor, no licence key, no third-party package. [plan]

## 2. Packet format

- Static payload types: 0 is PCMU and 8 is PCMA, clock rate 8000 Hz, one channel (RFC 3551; read in several secondary sources that cite it, the RFC itself not read). [read]
- Fixed header, the first 12 octets of every packet: version (2 bits), padding (1), extension (1), CSRC count (4), marker (1), payload type (7), sequence number (16), timestamp (32), SSRC (32); then CC times 4 octets of CSRC identifiers, an optional extension (a 16-bit profile id, a 16-bit length in 32-bit words, then that many words), the payload, and optional padding whose last octet is the padding count. The version is 2. (RFC 3550 sections 5.1 and 5.3.1; read in secondary sources, the RFC itself not read.) [read]
- A 20 ms packet is 160 payload octets at 8000 Hz with one byte per sample, and the timestamp advances by 160 per packet (arithmetic from the clock rate). Longer packets are allowed by the profile; the table sources give 20 ms as the default. [read]

## 3. Accepting a packet

A datagram is dropped and counted by reason unless all of these hold [plan]:

| Rule | Default |
|---|---|
| Length at least 12 octets and at most 1500 | fixed |
| Version is 2 | fixed |
| Header length (12 + 4 times CC) fits inside the datagram | fixed |
| Extension bit is clear (packets with an extension are dropped in this version) | until a real backend needs them |
| Padding, if set, has a count that fits and leaves a payload | fixed |
| Payload type equals the one the operator configured (0 or 8), never learned from packets | operator |
| Payload length between 1 and 480 octets (60 ms) | 480 |
| SSRC equals the first valid SSRC of the call; a different SSRC is dropped, not adopted | strict |
| Sequence number is not a duplicate and not older than the last accepted, compared modulo 65536 | fixed |

Telephone-event packets (DTMF, a dynamic payload type) fail the payload-type rule and are dropped until DTMF is designed. [plan]

## 4. Who may send, and limits

RTP over UDP cannot authenticate its sender. This endpoint does not claim to. What it does, as a requirement of phase0-interface.md (transport authentication): [plan]

- The operator configures the expected remote IP address (and optionally the port). Packets from any other source are dropped before parsing. The endpoint never latches onto the first source that sends, because that lets anyone who reaches the port take over the call.
- It listens on 127.0.0.1 unless the operator opts in to another address, and an opt-in prints a loud warning that the media is neither encrypted nor cryptographically authenticated. SRTP is out of scope for now (section 10).
- A missing or invalid configuration (no expected source, no payload type) is a startup error, never a fallback to accepting anything.
- Limits per call, all configurable: at most 100 packets per second (a 20 ms stream is 50), at most 3600 seconds of call length, and a call ends with a `call.ended` event after 10 seconds without a valid packet. The endpoint also limits concurrent calls (`Capabilities.max_concurrent_calls`). These numbers are untested starting points.
- Authentication of the call itself belongs to the signalling side: the SIP server decides which calls exist and tells the endpoint where to expect media.

## 5. Logging

Nothing is logged by default except counters: packets accepted, and packets dropped by each reason in section 3. No audio, payload, transcript or phone number ever appears in a log, an event or an error message, and neither does the remote IP address unless the operator turns that on. Error events carry a short fixed message. [plan]

## 6. Loss and order

No jitter buffer in this version. Duplicates and late packets are dropped (section 3). A gap in the sequence is filled with zero samples, at the PCM level so it does not depend on the law, up to 50 packets (1 s); a longer gap ends the fill and is counted. Whether this sounds acceptable, and whether recognition suffers, is untested. [plan]

## 7. Sending

Outbound audio goes through `OutboundAudio` and is cut into 160-octet packets sent at 20 ms intervals from a monotonic clock. Header: version 2, no padding, no extension, no CSRC, marker clear, the configured payload type, a sequence number and timestamp that start at random values and advance by 1 and 160, and a random SSRC, all made with the `secrets` module. (RFC 3550 recommends random initial values; from memory, to check at section 5.1.) [unverified] `clear_playback` drops every queued packet at once. `playback.finished` is sent when the queue has drained, which is the equivalent of a Twilio mark.

## 8. Fit with the Call contract

- `audio_in` yields the Frames produced by `InboundAudio` from accepted packets.
- `send_audio` feeds `OutboundAudio` and the sender queue.
- Events: `call.started` on the first valid packet, `playback.finished` as in section 7, `call.ended` on the timeout or limit in section 4, and `error` with a fixed message.
- `hangup` stops the media and emits `call.ended`. It cannot end the phone call: that needs a BYE from the signalling side. How the endpoint tells the SIP server to hang up is an open question (section 10), and until it is answered `hangup` must not be described as ending the call. [plan]

## 9. Tests

Unit tests use an in-memory transport and hand-built packets, never a socket and never a network. A loopback UDP test on 127.0.0.1 is a separate opt-in test, off by default. There are no real packet captures yet: parsing is tested against packets built by our own builder and against hand-written byte strings, which shows self-consistency and not interoperability. [plan]

## 10. Open questions

- DTMF: RFC 4733 telephone-event packets use a dynamic payload type negotiated in SDP, so they cannot be handled without SDP knowledge.
- SRTP and RTCP: not designed here.
- Symmetric RTP and NAT: a remote behind NAT may send from a different port than announced. Allowing a port range, or latching under an explicit opt-in, needs a threat discussion first.
- How a hangup reaches the SIP server.
- Whether a jitter buffer is needed, and how much loss recognition tolerates: needs measurement on real calls.
- Interoperability with Asterisk, FreeSWITCH and others: needs real captures and a throwaway test machine, never production.
