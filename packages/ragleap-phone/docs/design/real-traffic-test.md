# ragleap-phone: testing the RTP endpoint against real traffic (checklist, draft)

Status: a checklist, not run. Tags: [plan] intention, [unverified] from memory or not checked. Nothing in the package has been tested against a real SIP server so far (see the 2026-11-11 update in HANDOVER.md). This checklist is server-neutral on purpose: it does not assume Asterisk, FreeSWITCH or any other product, and it does not name a feature of any of them.

## 1. What this test is for

`RtpMediaEndpoint` and its per-packet rules (docs/design/phase0-rtp-endpoint.md) were written from the RFCs as read in secondary sources and tested with packets we built ourselves. That shows self-consistency and not interoperability. This test puts real traffic through it, to find out where the design is wrong and which of its untested numbers need changing. It does not test SIP signalling (not built), several calls (not built), DTMF or encryption. [plan]

## 2. Rules before you start

- Use a throwaway machine or container. Never the production server and never the shared server that runs the app. Work on loopback or on a private network; no SIP or RTP port may be reachable from the internet.
- No carrier, no real phone number and no real person's voice. Use a tone, synthetic speech, or your own voice with your own consent.
- Write down every version (server, operating system, Python, ragleap-phone commit) and the documentation page you followed, with the date.
- No keys, passwords or addresses in anything you commit or post.
- Stop and report instead of working around a problem: a feature that does not exist is a result.

## 3. Setup, server-neutral

1. Install one SIP server of your choice on the throwaway machine and note its name and version.
2. Find in that server's documentation a way to send a call's media as plain RTP to a UDP address and port you choose, and to receive plain RTP back from it. Which server and version offers this differs, so establish it first and record the page you read. If no such feature exists, say so in the report. [unverified]
3. Make a test call between two softphones or a test tool on the same private network, routed through that feature so that the media goes to the endpoint.
4. Choose the codec on the call: PCMU or PCMA, 8 kHz. The endpoint accepts only the payload type you configure (0 or 8).

## 4. Run the endpoint

The endpoint listens on 127.0.0.1 by default. If the SIP server is on another machine, pass `host="<private address>"` and `allow_non_loopback=True`, which warns that the media is unencrypted: only on a private network. This sketch runs on loopback, as tested on 2026-11-11 with a peer socket of our own:

```python
import asyncio
from ragleap_phone import EndpointConfig, RtpMediaEndpoint

async def on_call(call):
    async for frame in call.audio_in():
        pass  # look at call.counts afterwards; never print or store the audio

async def main():
    cfg = EndpointConfig(law="ulaw", remote_ip="<where the server sends media from>", remote_port=0)  # replace both
    ep = RtpMediaEndpoint(cfg, port=0)  # port: the one the server sends to; 0 lets the system choose
    await ep.run(on_call)  # ends after the idle timeout (10 s without a valid packet) or when the call is closed
    print({k: v for k, v in ep.call.counts.items() if v}, "socket errors:", ep.socket_errors)

asyncio.run(main())
```

`remote_ip` and `remote_port` must be the address and port the server really sends media from; the endpoint drops everything else before parsing.

## 5. What to look at

Capture the media with a packet capture tool, only the RTP port, for about 30 seconds of a call, and compare it with `call.counts`. Questions the real traffic should answer (each is an open question or an assumption of the design):

- Which payload type and packet size (ptime) does the server send? The endpoint accepts 1 to 480 payload octets and the configured type only.
- Is the header extension bit ever set? Packets with an extension are dropped.
- Does the SSRC change during the call, for example on hold, transfer or a media renegotiation? A different SSRC is dropped, not adopted.
- Is the source address and port of the media the one the server announced? Any other source is dropped (this is the NAT and symmetric-RTP question).
- Does the stream pause during silence, and does the marker bit mark the restart? A gap is filled with silence up to 50 packets.
- Do telephone-event packets appear when a key is pressed, and with which payload type? They are dropped for now.
- Does RTCP arrive, usually on the next port number? [unverified, from memory] The endpoint does not listen for it.
- Does anything arrive faster than 100 packets per second?
- Played back by the tester, does the received audio sound right, and does the audio sent back sound right? This is a listening impression, not a measurement.
- Which counters in `call.counts` are not zero after 30 seconds?

## 6. What never goes into the repository

Capture files, recordings, IP addresses, host names, phone numbers and keys. Share the counters and your observations in words. If a fixture is useful later, propose one separately: headers only, payload replaced with silence, addresses removed. No script for that exists yet.

## 7. Report (What / Limits / Tested / Not tested yet)

- What: the server and version, how the media was routed, the codec and packet size.
- Limits: one machine, one call, one operator; what was synthetic.
- Tested: exactly what was run, the `call.counts` after about 30 seconds, and each answer from section 5.
- Not tested yet: everything else, in particular signalling, several calls, DTMF, encryption and carriers.
