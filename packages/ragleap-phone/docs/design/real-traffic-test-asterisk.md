# ragleap-phone: real-traffic test with Asterisk 22, loopback only (draft runbook)

Status: a draft, not run. Written on 2026-11-16 from the documentation and source commits named below. Tags: [read] seen in a source page, [plan] an intention, [unverified] from memory or not checked. This complements real-traffic-test.md, the server-neutral checklist; its rules (section 2) apply here unchanged.

## 1. Choices

- Asterisk 22.x. The Asterisk Versions page (read 2026-11-16) lists 22.x as a long-term-support series released on 2024-10-16, with security fixes only from 2028-10-16 and end of life on 2029-10-16. [read] The same table lists 23.x as a standard series whose security-fix-only date (2026-10-15) has passed while its status column still says fully supported, so 23.x is not used. [read]
- A plain install on a throwaway Linux machine, not a container: the UDP port mapping of a container can change the source address and port of packets, and those are what this test looks at. [plan]
- Test A uses no SIP at all and works on loopback, so nothing needs to listen on a public port.
- Install Asterisk by the official installation page for that version. The installation steps were not read for this draft. Write down the exact version and how it was installed.

## 2. Test A: Asterisk's own RTP into our endpoint, no SIP

What this relies on:
- The Asterisk command line can start a call between a channel and a dialplan application: `channel originate <tech/data> application <appname> [appdata]`; calls started this way get a timeout of 30 seconds. [read, docs.asterisk.org, Operation, Creating and Manipulating Channels from the CLI]
- The dial string of the UnicastRTP channel is `UnicastRTP/<address:port>/c(<codec>)`, with c(ulaw) choosing the codec. [read in a 2016 code-review commit and the chan_rtp entry of CHANGES, not in the current documentation; unverified for 22.x] The channel sets the variables UNICASTRTP_LOCAL_ADDRESS and UNICASTRTP_LOCAL_PORT. [read in the same source]
- PlayTones plays a tone list while the dialplan goes on. [read] Whether it accepts a plain frequency such as 440 was not checked; see the sample indications.conf. [unverified]

Phase A1, observe with a dummy listener:
1. Run `ss -lun` and `ss -ltn` and note what listens. Nothing may listen on a public address.
2. Start a capture on the loopback interface: `tcpdump -i lo -n -w a1.pcap udp`.
3. Start a dummy UDP listener on 127.0.0.1 and a port of your choice, so that Asterisk's packets are not answered with errors. [plan]
4. In the Asterisk console: `channel originate UnicastRTP/127.0.0.1:<that port>/c(ulaw) application PlayTones 440`. Write down the exact command and everything Asterisk prints. [unverified syntax of the dial string]
5. While the tone plays: `core show channels`, then `core show channel <name>`; write down UNICASTRTP_LOCAL_ADDRESS and UNICASTRTP_LOCAL_PORT.
6. Stop the capture and read it with `tcpdump -nn -X -r a1.pcap`. Write down: the source address and port of the packets, and whether it equals the two variables; the payload type; the marker bit of the first packet; whether the header extension bit is ever set; whether the SSRC stays the same; the timestamp step and the sequence step per packet; the spacing between packets.

This answers, from real packets: from which address and port Asterisk sends, the extension bit, SSRC changes and the first marker bit (questions 2 and 4 of asterisk-notes.md section 6). It does not answer the silence question, because a tone has no silence.

Phase A2, our endpoint receives:
7. Start the endpoint on 127.0.0.1 and the same port, law ulaw, with the remote left out, as in the sketch of real-traffic-test.md section 4. Start a new call as in step 4. Each call has its own channel and so probably its own source port: read it as in step 5 and give it to the endpoint with expect_remote(). Until then every datagram is dropped and counted as remote_not_set; that is expected. [plan]
8. Afterwards read the counters and write them down: accepted, wrong_source, remote_not_set, resyncs, jump_dropped, long_gaps, fill_limited, ahead_of_clock, duplicate_or_late, payload_size. All packets after the remote is set should be accepted. Anything else is a result: report it, do not work around it.
9. Never print or store the audio; counters only.

## 3. Not in this draft

- Silence behaviour, DTMF and the hang-up path need a call that comes in through SIP or through ARI. [plan]
- Sending audio back to Asterisk needs a bridge. [plan]
- The externalMedia path needs an ARI application, which probably needs a WebSocket client for its events; the Python standard library has none. [unverified, plan: decide later]

## 4. Report back

The Asterisk version and how it was installed, every command used, the counters, and the findings of step 6. Nothing except loopback addresses. Do not commit the capture file.

## 5. First result: Test A on loopback (2026-11-18, one run, not a PBX or trunk)

Setup actually used: Asterisk 22.12.0 built from asterisk-22-current.tar.gz (sha256 f8b2ba98aac6c4ff3376bc0a2868bea6ed577ca684efb7ec739ae6e8ce6e35e2, downloaded on 2026-11-17 from downloads.asterisk.org; no published checksum was found to compare with, so this only shows a later change), on Ubuntu 24.04 under WSL2 on a Windows laptop. The prerequisite script (contrib/scripts/install_prereq install) asked no questions; ./configure --with-jansson-bundled --with-pjproject-bundled; make -j8 took 3 min 20 s; make install; make samples was not run.

Corrections to section 2 above:

- Asterisk does not start with an empty /etc/asterisk: it exited with "modules.conf invalid or missing". The files used: modules.conf with autoload=yes, logger.conf with one console line, an empty asterisk.conf, and extensions.conf with one test context.
- The dial string UnicastRTP/127.0.0.1:40000/c(ulaw) works in 22.12.0: the channel came up, direction sendrecv, native format ulaw. core show channeltypes also lists WebSocket and AudioSocket channel drivers.
- PlayTones(440) was accepted without indications.conf. A call originated straight into an application ends when the application returns, and PlayTones returns at once, so the call has to be kept up: [atest] with PlayTones(440), Wait(15), Hangup(), started with: channel originate UnicastRTP/127.0.0.1:40000/c(ulaw) extension s@atest.
- UNICASTRTP_LOCAL_ADDRESS and UNICASTRTP_LOCAL_PORT were set on the channel, and every packet came from exactly that address and port.

The stream (one call of 15 s, 749 datagrams; a capture with tcpdump and the receiving script agreed on the count):

- RTP version 2, no padding, no header extension, no CSRC, payload type 0, 160-byte payloads, one SSRC.
- Sequence +1 and timestamp +160 on every step (748 of 748); the marker bit was not set on any packet; the first timestamp was 160.
- Arrival gaps 19.2 to 20.6 ms (mean 20.0), none above 30 ms; about 880 sign changes per second in the payload, as for a 440 Hz tone.

Our receiver on the real packets (RtpCall fed by a script that gave the expected remote to expect_remote() once the port was read from the channel variable; the UDP wrapper RtpMediaEndpoint was not used): 645 accepted, 104 remote_not_set (before the port was set, as designed), 146 queue_full; no other counter was above zero. queue_full is consistent with the script not reading audio_in() during the run, so the bounded frame queue (500) filled; the audio handed on was exactly 499 frames of 320 samples.

Not tested: silence suppression, DTMF, hang-up from the signalling side, packet loss or jitter, sending audio back, SRTP, and any real PBX or trunk. Not explained: with no configuration and no call, Asterisk opened two UDP sockets on random ports (one IPv4, one IPv6); the module that does it was not identified.

## 6. Second result: DTMF, silence and the UDP endpoint (2026-11-18, one run per case, loopback, UnicastRTP route)

Same setup as section 5.

- DTMF: the dialplan application SendDTMF(159) ran, and with rtp set debug on Asterisk reported 299 packets sent in that call, all of payload type 0. No telephone-event packets exist on this route, so the receive-DTMF option of the endpoint (dtmf_payload_type) does not apply to it. Whether Asterisk gives the digits another way, for example as events, was not checked.
- Silence: this channel stops sending when nothing is played. In one call (tone, a pause of about 5 s with a digit command in it, tone) there was a single jump: sequence +1 and timestamp +39016 after 4878 ms. The receiver accepted it; its long_gaps counter went up once (the gap is capped at 1 s by design) and no other drop counter was above zero. Whether the first packet after the pause carried the marker bit was not recorded.
- The real UDP endpoint (RtpMediaEndpoint, the expected remote given with expect_remote() after run() had started, audio_in() read continuously):
  - Case A, a call of 15 s: 746 packets accepted, 3 remote_not_set, nothing else; 747 frames read (14.9 s of audio); socket errors 0; the call ended with idle_timeout 25.0 s after the start, 10 s after the audio stopped.
  - Case B, tone 3 s, silence 14 s, tone 3 s: the call ended with idle_timeout at 13.0 s, 148 frames (2.9 s) were read, and the second tone was never delivered.
- Consequence: with a sender that stops transmitting during silence, the idle timeout (10 s without a valid packet) ends the call on our side. Without signalling there is no way to tell a long silence from a dead call, so this is a design decision that is still open; the value 10 s remains an untested starting value.
- Closing our side stops the media only: Asterisk kept sending until its channels were hung up from the console, as the design notes say about hangup().
- Not tested: sending audio back to Asterisk, a hang-up from the signalling side, packet loss and jitter, SRTP, and any real PBX or trunk.
