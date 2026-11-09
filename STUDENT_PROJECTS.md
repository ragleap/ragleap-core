# Student & Contributor Project Ideas

RagLeap Core is a real, working self-hosted RAG engine — not a toy project.
Everything here is genuinely used, genuinely tested, and a real contribution
here is a real contribution to a live open-source project, not a throwaway
exercise.

These are scoped project ideas at three levels: **starter** (a few hours to
a weekend), **semester** (a real final-year or capstone project), and
**research-grade** (open-ended, good for a thesis or independent study).

If you're a professor considering recommending this repo to students, or a
student picking a project: open an issue tagged with the project name below,
or start a [Discussion](https://github.com/ragleap/ragleap-core/discussions), and we'll help scope it together.

---

## Starter projects (good first issue)

- **Add a new BYOK LLM provider** — pick any provider not yet live-tested
  (see issue #10) and confirm it works, or fix it if it doesn't.
- **Add Meta WhatsApp Cloud API** as a third WhatsApp provider (see #8).
- **Improve error messages** — audit `core/api.py` and `channels/*/router.py`
  for unclear error responses, make them more actionable.
- **Write a new example script** for `examples/` — something the current
  two don't cover (batch ingestion, comparing providers side-by-side, etc.)

## Semester / capstone projects

- **Voice: Deepgram + ElevenLabs support** (see #12) — real STT/TTS
  integration work, testable against the existing Twilio Media Streams
  pipeline in `channels/voice/`.
- **A local web UI** — a settings + chat page (OpenClaw-style Control UI),
  served locally, no multi-tenancy needed. Would let non-technical
  self-hosters configure `.env`-equivalent settings without editing files.
- **A new channel adapter** — Slack, Microsoft Teams, or SMS (Twilio),
  following the exact pattern in `channels/whatsapp/`, `channels/telegram/`,
  `channels/discord/` (a `router.py` with `handle_incoming_message()`
  calling `core.chat.ask()`).
- **Citation validation quality investigation** (see #9) — real applied
  NLP/evaluation work: why is a validated-correct answer being scored
  `valid=False`? Propose and test a fix.
- **Package `core/` as an installable Python package** (`pip install`-able,
  its own `pyproject.toml`) — real packaging/distribution engineering,
  a genuinely useful contribution toward a modular RagLeap ecosystem.

## Research-grade / open-ended

- **Alternative retrieval strategies** — hybrid search, re-ranking,
  query expansion — benchmarked against the current plain pgvector
  cosine-similarity approach in `core/retrieval.py`.
- **Embedding provider flexibility** — right now embeddings are Gemini-only
  because the schema is hardcoded to 3072 dimensions. Investigate a
  dimension-agnostic schema design that would let embeddings become BYOK
  too, the way generation already is.
- **Multi-language voice quality** — the Voice channel's known limitation
  (OpenAI TTS is English-tuned) is a real, unsolved problem. A proper
  language-detection + provider-routing solution is genuinely open research.
- **Vertical AI Employee roles with enforced safety guardrails** — the
  Autonomous Loop (see ROADMAP.md) makes RagLeap's core capable of real
  agentic actions today, not just Q&A. The open research question is how
  a domain-specific role (healthcare, financial, legal) can have its data
  handling *validated* to stay within a defined boundary, rather than
  just trusting the system prompt — see `SENSITIVE_DOMAIN_ROLES` and the
  "Vision: Vertical AI Employees" section in ROADMAP.md for context.

---

## Why contribute here specifically

- Every merge goes through real CI (compile check + Docker build + smoke
  test) — your work gets tested the same way a professional team would
  test it, not just eyeballed.
- This isn't a toy or a tutorial project — it's the real, open-sourced
  engine behind a production SaaS platform (ragleap.com).
- Real, visible history: every PR, every fix, every discussion is public
  and permanent — genuinely something to point to.

Questions? Open a [Discussion](https://github.com/ragleap/ragleap-core/discussions) — happy to help scope
any of these to the right size for a course project, thesis, or weekend hack.
