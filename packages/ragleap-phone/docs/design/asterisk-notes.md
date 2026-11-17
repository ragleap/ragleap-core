# ragleap-phone: notes on connecting Asterisk (reading notes, draft)

Status: reading notes dated 2026-11-13. Nothing here was run. Tags: [read, docs] read in Asterisk's own documentation, [read, third party] read in a third-party page or repository, [read, code] read in the code of a repository, [unverified] not checked or from memory. Asterisk's documentation pages are licensed CC BY-SA 3.0, so this note paraphrases them and copies nothing.

## 1. What this is for

The real-traffic test (docs/design/real-traffic-test.md) needs a SIP server that can send a call's media to our `RtpMediaEndpoint`. This note records what Asterisk's documentation says about ways to do that, what it means for our code, and what a similar open project does. It is input for the test, not a recommendation to use Asterisk.

## 2. Routes out of Asterisk

- ARI `externalMedia`, RTP over UDP. [read, docs] A POST to `/channels/externalMedia` creates a channel for exchanging media with an external program; it was introduced in Asterisk 16.6. Required: the Stasis application, the media destination as host and port, and the format. There is no negotiation: Asterisk transcodes the bridge's media into the requested format (the page names `ulaw` and `g722` as examples). The page describes the first release as RTP over UDP only, with Asterisk as the connecting side and media in both directions, and says other transports were expected later, so newer versions may offer more. The channel driver underneath sets two variables, `UNICASTRTP_LOCAL_ADDRESS` and `UNICASTRTP_LOCAL_PORT`, with the address and port that media can be sent to; they can be read through ARI. Deleting the channel hangs up that channel.
- `chan_websocket`. [read, docs] Media goes over a WebSocket: binary frames carry media, text frames carry control events and commands. TLS is supported and most codecs are. Asterisk inserts silence to keep timing when the application sends nothing, and either side can open the connection (which helps with NAT). The connection announcement carries the format and an optimal frame size (160 octets for ulaw at 20 ms), and a flow-control event is sent when the outbound queue reaches about 900 frames. Available since 20.16.0, 21.11.0, 22.6.0 and 23.0.0.
- Events and commands of `chan_websocket` as listed by a third-party Rust crate (MIT or Apache-2.0 per its own page): DTMF end, media XOFF and XON, queue drained, errors, a command to set the media direction, and others. [read, third party] The primary protocol page was not read in full.
- AudioSocket over TCP also appears among the `externalMedia` options in Asterisk 18.5 reference documentation (a function name in the argument list). Its protocol was not read. [unverified]

## 3. What this means for RtpMediaEndpoint

- The first route is the documented counterpart of our plain-RTP endpoint. Our code handles G.711 only (`ulaw` and `alaw`, the names the `format` parameter takes). [read, docs]
- Ordering gap, handled in code on 2026-11-13. With `externalMedia` we first tell Asterisk where we listen, and only then can we read the address and port Asterisk reports. The endpoint therefore accepts the remote at creation or once afterwards through `expect_remote(ip, port)`; until then every datagram is dropped and counted (`remote_not_set`) and nothing is sent; nothing is ever learned from packets, so the no-latching rule stays. [tested with a fake transport, not against Asterisk]
- Source port. [unverified] The variables above give the port media can be sent to. Whether Asterisk sends its own RTP from that same port is not stated in what was read. If it sends from another port, our strict source check drops everything. The test must record the source port of the first packet.
- Hangup. [unverified] Deleting the external-media channel ends that channel, not the caller's call. Ending the call would need an ARI hangup of the caller's channel. ARI is REST plus an event WebSocket; plain REST needs only the standard library, but the event WebSocket needs a WebSocket client, which the standard library does not have.
- Silence and DTMF on the RTP route. [unverified] Not read: whether RTP keeps flowing during silence, and how DTMF arrives. Both affect the gap rule and the dropped telephone-event packets in docs/design/phase0-rtp-endpoint.md.
- The WebSocket route would need a WebSocket server (handshake, framing, size limits), written by us if the core stays standard-library only. That is a larger piece with its own security surface, and it ties us to Asterisk 20.16 or later. [plan]

## 4. What Dograh shows

Read on 2026-11-12 and 2026-11-13: the GitHub licence field and the LICENSE file say BSD-2-Clause. Nothing was run and no code is copied. [read, code]

- Its documentation for Asterisk asks for ARI enabled and for `chan_websocket` and `res_websocket_client`, and says it sends and receives audio over a WebSocket (Asterisk 22 or later, or Asterisk 20 LTS with those modules, are described as known to work).
- The ARI provider lives in `api/services/telephony/providers/ari/` with a transport, call strategies, a channel registry and an adapter for an external PBX, and there is a separate ARI manager service. The transport is built on Pipecat's FastAPI WebSocket transport.
- Transfer and hangup go through ARI REST calls; a comment says the code re-checks every 0.15 s whether the external PBX has dropped the channel yet.
- Its quickstart starts a public tunnel to the API for inbound telephony webhooks and ships its own language, speech-to-text and text-to-speech stack; whether that stack is local or hosted was not read. We do not copy either default: ours is loopback and no hosted dependency.
- Reading: media travels on one path and call control on ARI. That split is worth testing for ourselves.

## 5. Other open projects, with licence status

| Project | Licence status |
|---|---|
| Dograh | BSD-2-Clause, read at the source (field and LICENSE file) |
| Pipecat | BSD-2-Clause per third-party summaries; its licence file was not read |
| asterisk-rs-ari (Rust) | MIT or Apache-2.0 per its own page |
| galcymedia (Python, for `chan_websocket`) | licence not read |
| asterisk/asterisk-websocket-examples | licence not read |
| Hume SDKs (audio decisions, expression measurement) | MIT; clients for hosted paid APIs, so never in the default path |
| Hume `tada` (speech model) | code MIT; model weights under the Llama 3.2 Community License per the repository README; reviews calling it MIT conflict with the README; not on our allowlist; vendor claims untested |

`hume-typescript-sdk` does not exist under that name. [read, third party]

## 6. Questions for the throwaway-machine test

- Which Asterisk version, and does `externalMedia` with `rtp` and `udp` work as described?
- From which address and port does Asterisk send its RTP, and is it the port in the channel variable?
- Is the RTP stream continuous during silence? Does the marker bit or a timestamp jump appear?
- Is the header extension bit ever set? Does the SSRC change during the call?
- How does DTMF arrive on this route, and with which payload type?
- Which ARI call ends the caller's call, and how long does it take?

## 7. Rules

Understand the ideas and copy no code. Read every licence at its source page before any reuse. Never probe a hosted service, and run no comparison against one without the maintainer's own keys and go. Nothing from a hosted service enters the default path.

## 8. Answers read in the Asterisk documentation (2026-11-16, not run)

Source: docs.asterisk.org, the page "External Media and ARI" (Asterisk Framework and API Examples). Read, not tested. The page describes the first release of the feature, so a later version may differ. Everything here stays [unverified] until the throwaway-machine test in section 6.

- Version: the page says the externalMedia resource arrived in Asterisk 16.6. The documentation site lists Asterisk 18 to 24 and certified 18.9, 20.7 and 22.8; which versions are still supported was not read.
- How it works: an ARI application creates the channel with POST /channels/externalMedia, giving the application name, the media destination as host:port (external_host) and a format. Asterisk then sends RTP to that destination, opening the stream itself (client mode), and accepts RTP back. The channel is normally added to a bridge.
- Codec: the format is required and nothing is negotiated; Asterisk converts the bridge's media to the format named (the page's examples are ulaw and g722). ulaw or alaw matches the payload types 0 and 8 of this package.
- Limits stated for the first release: rtp over UDP only, direction both only, and no server mode (where Asterisk would give an address to connect to).
- Where to send media back: the channel driver sets the channel variables UNICASTRTP_LOCAL_ADDRESS and UNICASTRTP_LOCAL_PORT to the address and port that media can be sent to. The page does not say that Asterisk sends from that same address and port, so the expected remote of the source check (expect_remote) still has to be found from a capture.
- Hang-up: the page says to delete the external media channel to hang it up. It does not say whether that ends the caller's call; ending the caller's own channel through ARI was not read.
- Not stated on the page, so still open: whether RTP keeps flowing during silence, the marker bit, the header extension bit, SSRC changes, and how DTMF arrives. For DTMF it is open whether digits come as telephone-event RTP on this route or only as ARI events on the caller's channel; if only as events, the receive-DTMF option of the endpoint does not apply to this backend.

## 9. Versions and two more findings (read on 2026-11-16, not run)

- The Asterisk Versions page lists 22.x as a long-term-support series (released 2024-10-16, security fixes only from 2028-10-16, end of life 2029-10-16) and 23.x as a standard series whose security-fix-only date, 2026-10-15, has passed although the status column still says fully supported. The excerpt read did not include the rows for 20 and 21. The test runbook (real-traffic-test-asterisk.md) uses 22.x.
- Besides the ARI route, the dialplan can open a UnicastRTP channel directly: the dial string UnicastRTP/<address:port>/c(<codec>) is described in a 2016 code-review commit and the chan_rtp entry of CHANGES, not in the current documentation; unverified for 22.x.
- A community forum post reports that, in one setup, the variables UNICASTRTP_LOCAL_ADDRESS and UNICASTRTP_LOCAL_PORT were not set on an externalMedia channel. This is one user's report, unverified, and it conflicts with the page quoted in section 8; the test decides.
