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
