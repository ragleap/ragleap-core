# ragleap-phone: handover for the new owner

Written October 2026 for the colleague who will lead `ragleap-phone`. It is self-contained: you should not need to ask for background. It contains no secrets and must never get any (no keys, no server addresses, no phone numbers).

Evidence tags: **[verified]** seen in the repo or in public project documentation; **[plan]** an intention, not built; **[unverified]** believed but not checked. Please keep using these tags in your own notes and pull requests.

## 1. The goal in one paragraph

RagLeap answers phone calls today through Twilio. The project promises independence, so phone calls must work without Twilio, and as independently as possible. The work is a new library, `ragleap-phone`, that handles calls, SIP and audio, while speech (speech-to-text and text-to-speech) stays in `ragleap-voice`. Contributors from every country will help, country by country.

## 2. Decisions already made

1. **Separate library** `ragleap-phone`, not part of `ragleap-voice`. Reasons: phone handling and speech models are different jobs; a SIP stack should not be forced on people who only need speech; phone code opens network ports to the internet and needs its own security review and release cycle. [plan]
2. `ragleap-phone` depends on `ragleap-voice` for speech, through the speech engines API proposed in issue #762 (still a proposal). [plan]
3. **Twilio stays** as one adapter so nothing breaks. [plan]
4. "Independent" means independent of Twilio and of any single provider, not of carriers (see section 4).
5. A public design issue (RFC) opens the work and calls for country champions. It is live: issue #814, https://github.com/antonyrag/ragleap-core/issues/814 (labels `voice`, `help wanted`, `good first issue`).
6. Public claims need evidence. The README promise is "built in public, no vaporware". Every claim needs a real test, and pull requests list honest limits.

## 3. What exists today

- `channels/voice/` [verified]: `router.py` handles Twilio Media Streams over a WebSocket (mu-law 8 kHz audio in and out), `audio.py` converts audio, `server.py` runs a separate server because Twilio streams audio in real time. Twilio's TwiML points at that server's address. About 325 lines in total.
- Speech in that channel uses OpenAI's hosted speech-to-text and text-to-speech only [verified]. So a call currently depends on Twilio and on OpenAI. Real independence also needs local speech, which is `ragleap-voice`'s job.
- `ragleap-voice` [verified]: a pre-alpha package for managing open, local speech models (licence-checked manifests, downloads, benchmarks). Since 0.1.0 it has `transcribe()`, `synthesize()`, a Whisper engine and an Indic Parler-TTS engine; every model is still unverified. Streaming building blocks (stateful resamplers, a stream gate, a batch-adapter `transcribe_stream`) are on main and not yet released.
- The voice server is its own compose service (`voice`). Nothing in the app changes unless voice is switched on.

Related issues: #814 (the public RFC for this work), #762 (speech engines API proposal), #763 (consent store and voice-profile design, aware of India's data protection law: relevant to call recording), #801 (number words for every language, used when scoring speech).

## 4. What "independent" can honestly mean

A real phone number needs a carrier or a telecom line. Software alone cannot join the phone network. Three levels:

1. **Any SIP trunk.** Standard SIP; swap carriers freely; no Twilio code.
2. **Own PBX and lines on premises.** No cloud telephony provider in between. Fits banks and research institutes that want local models and possibly no internet.
3. **No phone network at all.** SIP or web calls between apps. Fully independent, but cannot reach ordinary phones.

Telephony law differs by country, and some countries restrict connecting internet calls to the phone network. Nobody on this project is giving legal advice: each country needs its own check, with sources. **Emergency calling is never supported**, and the README must say so.

## 5. Open-source projects to study

Verified in public documentation:
- **jambonz**: MIT licence, self-hosted, connects to any SIP trunk or PBX, is documented as able to run air-gapped, but it needs a licence key tied to a domain and whether it works offline is unverified (see the Phase 0 design). Built on drachtio, rtpengine (GPL v3) and FreeSWITCH (MPL). A community Python SDK exists.
- **LiveKit**: Apache 2.0, SIP integration for agents that place and receive calls, self-hostable. How complete its self-hosted SIP is has not been verified.

From memory, not rechecked: Asterisk, FreeSWITCH, Kamailio, OpenSIPS, Pipecat, PJSIP and baresip.

Rule: understand their ideas, do not copy code. Run them as separate services. Review every licence before bundling anything, especially GPL components, and record third-party components in a `THIRD_PARTY_NOTICES.md` as `ragleap-voice` does. No backend has been chosen. Choosing one is phase 0.

## 6. Proposed phases (each is done only with evidence)

| Phase | What | Done when |
|---|---|---|
| 0 | Design: compare 2 to 3 backends on licence, latency, audio path, security; write the call flow; list country rules to check | A written design merged, with the comparison and the reasons |
| 1 | A provider interface; move the Twilio code into one adapter | Existing behaviour unchanged, tests pass, no new claims |
| 2 | A self-hosted SIP backend behind the same interface | A real call answered through a SIP trunk, with a screenshot or video in the pull request |
| 3 | Local speech through `ragleap-voice` | A real call with no hosted speech, with measured latency |
| 4 | On-premises guide for banks and institutes | A documented install tested by someone outside the project |

## 7. Security and legal checklist

- SIP exposed to the internet attracts spam calls and toll fraud. Plan authentication, rate limits and allow-lists from phase 0.
- Calls contain personal data. By default, do not log audio, transcripts or phone numbers. Recording and voice cloning need explicit consent and per-country checks (see #763).
- Verify every inbound webhook or signalling source. Whether the current Twilio channel validates Twilio's request signature has not been checked: check it. [unverified]
- Never put keys, real phone numbers or recordings in the repository, issues, logs or screenshots.
- Report vulnerabilities privately through the repository's Security tab (see `SECURITY.md`), not in public issues.

## 8. Country champions (the call for contributors)

Each country can have a champion who contributes any of these:
- a guide to SIP trunks and carriers in the country, with the date written, no endorsements;
- notes on telephony and call-recording rules, with sources;
- a real test call report with a screenshot or short video (hide keys and numbers);
- local number formats and number words (#801) and local speech models.

Rules: one champion per country at a time; a claim quiet for a few weeks may be taken by someone else; everyone who helps is credited by name on the website and social media, unless they opt out. Keep a table in the package docs: country, champion, what is done, link to the report. It starts empty and fills only from real reports.

## 9. Coding style and conventions

Rules that exist in the repository today [verified]:
- Python, PEP 8-ish, not strictly enforced; keep it readable. Add a short docstring to new functions, especially when behaviour is not obvious.
- Branches `feat/...`, `fix/...`, `docs/...`. Commit messages start with `feat:`, `fix:` or `docs:`. Small, focused pull requests.
- Package layout as in `packages/ragleap-voice`: `pyproject.toml` with hatchling, `src/ragleap_phone/` layout, `tests/` with pytest, `README.md`, `LICENSE` (MIT), `CHANGELOG.md` in Keep a Changelog style, `THIRD_PARTY_NOTICES.md`, optional `docs/` and `examples/`. Python 3.10 or newer. Pre-alpha classifier until real calls are tested.
- The core stays standard-library only; heavy engines arrive as optional extras, as in `ragleap-voice`.
- CI must pass: `compile-check` and `docker-build-and-smoke-test` are required; about 37 checks run in total. Third-party GitHub Actions are pinned to commit hashes; keep it that way.

Proposed for this package (not repository rules yet; agree them in the first pull request) [plan]:
- Type hints on public functions; the adapter interface as a small abstract class, with Twilio and SIP as implementations.
- Unit tests never make real calls or network requests. Anything that needs a trunk is a separate opt-in test, off by default.
- New opt-in environment flags are off by default and are added to `_FEATURE_FLAGS` in `tests/conftest.py`.
- Pull request description in four parts: **What / Limits / Tested / Not tested yet**. Be honest in Limits.
- Never mark something tested without a real run. Say exactly what was run.

## 10. How to work with the maintainer

- The maintainer (Antony) does the public and production steps: merging, releases, posting issues, publishing to PyPI, changing repository settings, spending money. Ask for an explicit go before any of those.
- Changes to production, the Docker socket, the sandbox or network exposure get extra care: write what could go wrong, and how to roll back.
- The voice work has its own lead. Talk to that person first about the engines API (#762) so `ragleap-phone` and `ragleap-voice` agree on one interface (audio frames in and out, 8 kHz telephone audio versus 16 kHz speech models, call events, DTMF digits).

## 11. First week

1. Read this file, `channels/voice/`, `packages/ragleap-voice/`, `SECURITY.md` and `CONTRIBUTING.md`.
2. Read the RFC issue and answer the first comments; welcome the first champions.
3. Meet the voice lead about #762; write down the audio and event interface you both need.
4. Phase 0: study two backends hands-on on a throwaway machine (never production), write the comparison.
5. Open the first code pull request: the package skeleton (`pyproject.toml`, `src/ragleap_phone/`, tests). This file already lives in `packages/ragleap-phone/docs/`.

## 12. Open questions (nobody knows yet)

- Which backend, and how to run it in the Docker setup.
- Which country rules apply for India first, and for banks that run without internet.
- Which SIP trunk and number to use for test calls, and who pays for them.
- Whether local speech is fast enough on 8 kHz phone audio, and how to handle echo and interruptions (barge-in).
- Whether Twilio request signatures are validated in the current channel.

## 13. Where this lives

This file lives at `packages/ragleap-phone/docs/HANDOVER.md` in the repository, next to a short README that says the package is planned and has no code yet. It is public, so it must stay free of anything private. Private matters (access, accounts, numbers) are passed to you directly, never written in the repository.

## Updates (2026-10-21)

Added by the current owner; the text above is unchanged. Evidence tags as in section 0.

- [verified] The jambonz repositories listed in the design note (api-server, feature-server, sbc-inbound, sbc-outbound, sbc-sip-sidecar, sbc-rtpengine-sidecar, webapp) and drachtio-server carry the MIT licence on GitHub; rtpengine is GPL-3.0; LiveKit server and LiveKit SIP are Apache-2.0.
- [verified in jambonz documentation, https://docs.jambonz.org/self-hosting/licensing and https://docs.jambonz.org/self-hosting] A self-hosted jambonz system requires a software licence key tied to a domain name. A free trial key lasts 30 days; after that a paid licence is needed. Extended free licences are offered for non-commercial use and pre-revenue startups on request. The statement in section 5 that jambonz is "MIT licence, self-hosted, ... can run air-gapped" is therefore only partly right: it holds for the open repositories and for what the jambonz overview page says, not for the packaged self-hosted product.
- [unverified] Whether the open repositories run without the key, whether the key is checked online, and whether mediajam (the media server since jambonz 11) is open. These decide whether jambonz can meet the project's independence goal.
- [verified, https://docs.jambonz.org/self-hosting/overview/krisp-turn-taking] Krisp features need a separate Krisp API key and outbound HTTPS to a Krisp host for self-hosted jambonz; RNNoise is offered as the open option.
- The RFC issue for section 2 is #814.

### Further updates (2026-10-21)

- [verified, GitHub, 2026-10-21] The public jambonz-feature-server repository is the 0.9.x line (package.json version 0.9.14, MIT, depends on drachtio-fsmrf; its README sets a FreeSWITCH address and lists no licence-key setting). The jambonz documentation describes a different 11.x line with a media server called mediajam and a licence key. No repository named mediajam exists in the jambonz GitHub organisation's public list.
- [verified, GitHub, 2026-10-21] The jambonz organisation's python-sdk repository declares no licence, so the line in section 5 saying "a community Python SDK exists" should read: exists, licence not declared (not reusable until one is added).
- [verified, GitHub, 2026-10-21] heplify-server (AGPL-3.0) and the rtpengine repository in the jambonz organisation (GPL-3.0; whether it is a fork was not checked) are copyleft; Asterisk, Kamailio and OpenSIPS are GPL-licensed (see the design note). None may be bundled without a licence review; running them as separate services is the plan.
- [unverified] Whether the open 0.9.x line runs without any licence key or online check, and whether it is maintained to parity with the 11.x line. A hands-on test on a throwaway machine is needed.
