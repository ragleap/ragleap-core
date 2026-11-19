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
| Sequence number: up to 3000 ahead is accepted; up to 100 behind is dropped as late; a larger jump in either direction is dropped until a second packet follows it consecutively, then the call resynchronises (values from memory of RFC 3550 appendix A.1, unverified) | starting values |

Telephone-event packets (DTMF, RFC 4733) fail the payload-type rule and are dropped unless the operator sets dtmf_payload_type; see the section on DTMF at the end of this note.

## 4. Who may send, and limits

RTP over UDP cannot authenticate its sender. This endpoint does not claim to. What it does, as a requirement of phase0-interface.md (transport authentication): [plan]

- The operator configures the expected remote IP address and port, either when creating the endpoint or once afterwards with `expect_remote()` (the order Asterisk's `externalMedia` needs: we listen first and learn the other side's port later). Until the remote is set, every datagram is dropped and counted (`remote_not_set`) before parsing, and nothing is sent. Packets from any other source are dropped before parsing. The endpoint never latches onto the first source that sends, because that lets anyone who reaches the port take over the call.
- It listens on 127.0.0.1 unless the operator opts in to another address, and an opt-in prints a loud warning that the media is neither encrypted nor cryptographically authenticated. SRTP is out of scope for now (section 10).
- A missing or invalid configuration (no expected source, no payload type) is a startup error, never a fallback to accepting anything.
- Limits per call, all configurable: at most 100 packets per second (a 20 ms stream is 50), at most 3600 seconds of call length, and a call ends with a `call.ended` event after 10 seconds without a valid packet. The endpoint also limits concurrent calls (`Capabilities.max_concurrent_calls`). These numbers are untested starting points.
- Authentication of the call itself belongs to the signalling side: the SIP server decides which calls exist and tells the endpoint where to expect media.

## 5. Logging

Nothing is logged by default except counters: packets accepted, and packets dropped by each reason in section 3. No audio, payload, transcript or phone number ever appears in a log, an event or an error message, and neither does the remote IP address unless the operator turns that on. Error events carry a short fixed message. [plan]

## 6. Loss and order

No jitter buffer in this version. Duplicates and late packets are dropped (section 3). A gap is worked out from the timestamps, not from the sequence numbers: the samples not received before a packet are its timestamp step minus the length of the previous packet, so it covers lost packets and also silence suppression, where the sequence continues but the timestamp jumps. The gap is filled with zero samples, at the PCM level so it does not depend on the law, up to 50 packets' worth (1 s); a longer gap is capped and counted. No gap is filled at the first packet of a call (there is no earlier timestamp to measure from) or at a resynchronisation: the packet that confirms a sequence jump is accepted with a gap of 0 and the first packet after the jump is dropped, so a sequence jump together with a timestamp jump produces no silence and costs one packet of audio. The timestamp base after such a jump may be unrelated to the old stream, so a gap worked out from it would mean nothing. Checked on 2026-11-16 in one scratch run with synthetic packets: a one-second timestamp jump with a continuing sequence gave 8640 samples handed on, the same jump with a sequence jump gave 640. The total audio handed on in a call is also limited to the elapsed real time plus 2 s of slack (a stream cannot carry more audio than real time), so timestamp jumps from a hostile sender cannot multiply the work downstream; the excess is counted as `fill_limited`. The same limit covers the payload: a packet whose samples, together with its gap, do not fit in the remaining budget is dropped and counted as `ahead_of_clock` (the receiver's own counters still count it as accepted). Found by a review on 2026-11-16: before this change the limit covered only the gap, so a sender that passed the source check, at the default 100 packets per second of 480 bytes with no gaps, got 59.5 s of audio counted in 10 s of real time (about 6 times), and the used-up budget then cut a legitimate later silence; the frame queue is bounded, so the excess was dropped there and not stored. One scratch run with synthetic packets, not a benchmark. Found by an outside review on 2026-11-13: in a scratch run with synthetic packets, 100 packets in one second produced 101 s of audio before this limit and 4 s after. A long run of silence is written out without filtering, which gives the same samples and costs almost nothing. Whether this sounds acceptable, and whether recognition suffers, is untested. [plan] Found by running the code on 2026-11-11: before this change a sequence restart silenced a call for about 100 s, one 50-packet gap cost 0.026 s of CPU (100 such packets in a second cost 2.65 s) and a 2 s silence suppression produced no gap at all. Those are numbers from one scratch run with synthetic packets, not a benchmark.

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

- DTMF: RFC 4733 telephone-event packets use a dynamic payload type negotiated in SDP. This package has no SDP, so the operator gives the number as dtmf_payload_type (off by default); see the section on DTMF at the end of this note.
- SRTP and RTCP: not designed here.
- Symmetric RTP and NAT: a remote behind NAT may send from a different port than announced. Allowing a port range, or latching under an explicit opt-in, needs a threat discussion first.
- How a hangup reaches the SIP server.
- Whether a jitter buffer is needed, and how much loss recognition tolerates: needs measurement on real calls.
- Interoperability with Asterisk, FreeSWITCH and others: needs real captures and a throwaway test machine, never production.
- Injection by a sender that passes the source check: RTP without SRTP cannot stop it. Such a sender can inject audio, and can also keep the call in the resynchronisation state by interleaving packets with the right SSRC (in a scratch run, 0 of 200 legitimate packets were accepted while it did). The rate limit bounds the volume; only encryption and authentication of the media (SRTP) or a trusted network path address the cause.


## DTMF (RFC 4733), receive only

Read on 2026-11-16 at rfc-editor.org (RFC 4733, sections 2, 3, 5 and 6). Off by default. Not tested with any real traffic, PBX or trunk.

- The operator sets dtmf_payload_type (a dynamic type from 96 to 127, the number agreed out of band) in EndpointConfig; this package has no SDP. With the default (None) every telephone-event packet fails the payload-type rule as before.
- A report must have a payload of exactly 4 octets (packed reports and redundant payloads are refused and counted as payload_size). Event codes 0 to 15 are digits; other codes, and a duration of 0, are ignored and counted as dtmf_ignored. The reserved bit and the volume are ignored.
- Reports share the sequence numbers of the audio, so the sequence rules apply to them. They do not move the audio timestamps, so the gap before the next audio packet still covers the time of the digit and is filled as silence. A report never takes part in a resynchronisation and never fixes the SSRC of the call: one that arrives before any audio packet is dropped (dtmf_before_audio).
- One digit gives one dtmf event, on the first report seen for it. Updates, the repeated final reports and the next segment of a long digit (timestamp equal to the previous timestamp plus 0xFFFF after a report of duration 0xFFFF without the end bit) are not new digits. A short history of recent (timestamp, event) pairs stops a late repeat from counting twice.
- Limits (starting values, untested): max_dtmf_per_second 10 (RFC 4733 section 3.1 cites 8 to 10 digits per second for legacy equipment) and DTMF_BACKLOG 32 events waiting unread; the events queue itself has no size limit, so this backlog limit is what keeps a hostile sender from growing it. Digits past either limit are dropped and counted (dtmf_limited, dtmf_backlog). Reports also count in the packet rate limit and are not audio: they never use the real-time budget.
- Counters hold no digit. The digit appears only in the event that is handed on, and is never logged.
- No integrity: RFC 4733 section 6 says integrity must be provided for this payload, and names SRTP, which this package does not have. A sender that passes the source check can forge digits. Do not use DTMF for secrets such as PINs until SRTP exists.
- Tests use the example of RFC 4733 section 5 (the digits 9, 1, 1; the timestamps, durations and repeated final reports of its Table 5; the rows the table leaves out are filled in by its pattern) and hand-built bytes: this shows self-consistency, not interoperability.
- The class-level flag capabilities.dtmf of RtpMediaEndpoint stays False because DTMF is off unless the operator turns it on. Not built: sending digits, packed reports, RFC 2198 redundant payloads, tone payloads.

## Idle timeout on a route that stops sending in silence (measured 2026-11-18)

Measured once on loopback with the UnicastRTP channel of Asterisk 22.12.0 (details in real-traffic-test-asterisk.md, section 6): when the sender stopped transmitting for 14 s, the call ended by idle_timeout 10 s after the last packet and the audio that followed was never delivered. The default of 10 s is therefore not safe for such a sender. No change was made; what to do (a longer value, a value per backend, or a liveness signal from the signalling side) is open.
