# Contributing to RagLeap Core

Thanks for your interest in contributing! Here's where things actually stand
and how to get started.

## Where things stand

RagLeap Core is a working, tested self-hosted RAG engine with document
ingestion, retrieval, generation, and WhatsApp/Telegram/Discord/Voice
channel adapters — all verified end-to-end. Every change goes through CI
(compile check + a real Docker build + smoke test), so contributions are
welcome now, not "someday."

Check the [good first issue](https://github.com/ragleap/ragleap-core/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
label for scoped, approachable tasks, or
[help wanted](https://github.com/ragleap/ragleap-core/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22)
for things that need investigation or specific API access to test.

## How to contribute code

1. Fork the repo and clone your fork
2. Create a branch: `git checkout -b feat/your-change` (or `fix/`, `docs/`)
3. Make your change
4. Test it locally: `docker compose up --build -d`, then verify with a real
   request (`curl http://localhost:8000/health`, or whatever your change
   affects)
5. Open a Pull Request — CI will run automatically (compile check + Docker
   build + smoke test)
6. Once checks pass, a maintainer will review and merge

Small, focused PRs are much easier to review than large ones — if your
change touches multiple unrelated things, consider splitting it up.

## Code style

- Python, PEP 8-ish (not strictly enforced yet, but keep it readable)
- Clear commit messages: `feat: ...`, `fix: ...`, `docs: ...` prefixes are
  used throughout the existing history — following that pattern helps
- Add a brief docstring to new functions, especially anything with
  non-obvious behavior (see `channels/*/router.py` for examples)

## Reporting bugs

Open an [Issue](https://github.com/ragleap/ragleap-core/issues) with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your environment (OS, Docker version, `LLM_PROVIDER` if relevant)

## Adding a new BYOK provider or channel

If you're adding support for a new LLM provider or messaging channel,
look at the existing pattern first:
- LLM providers: `core/generation.py`'s `PROVIDER_BASE_URLS` dict and
  `GenerationService` class
- Channels: `channels/whatsapp/`, `channels/telegram/`, or
  `channels/discord/` — each follows the same shape (a `router.py` with
  `handle_incoming_message()` calling `core.chat.ask()`)

## Code of Conduct

Be respectful and constructive. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Adding an AI Employee

There are two ways to add an employee. Pick the one that fits.

### A. A built-in role (ships with every install)

Edit `core/employees/defaults.py`:

1. Add the role name to `ROLE_CHOICES`.
2. Add an entry to `DEFAULT_ROLES` with `role`, `display_name`, `channels`,
   `skill_tags` and a real `personality` (at least a couple of sentences).
3. Add the same role name to `ROLE_SKILL_TAGS` with the tags used to find its memory.
4. If the role touches health, legal, tax, immigration, insurance or compliance
   work, also add it to `SENSITIVE_DOMAIN_ROLES` and add a matching compliance
   seed to `DEFAULT_COMPLIANCE_MEMORY_SEEDS` (one seed per sensitive role).

Then run:

```bash
python3 -m pytest tests/test_role_definitions.py
```

These tests fail with a clear message if a list is out of sync, if a field is
missing, or if a role carries a sensitive-domain tag (legal, compliance,
healthcare, medical, clinical, immigration, regulatory) without being listed in
`SENSITIVE_DOMAIN_ROLES`. If you have reviewed a role and it is genuinely not
sensitive, list it in `REVIEWED_NOT_SENSITIVE` in that test file with a reason.

### B. A custom role at runtime (no code change)

```bash
curl -X PATCH http://localhost:8000/employees/pizza_bot \
  -H "Content-Type: application/json" \
  -d '{"display_name": "Pizza Bot", "channels": ["whatsapp"],
       "skill_tags": ["menu", "orders"], "personality": "You are the ordering assistant for..."}'
```

The response includes `sensitive_domain`. A runtime role whose name, display
name or skill tags contain a marker word (legal, lawyer, medical, health, tax,
immigration, insurance, compliance and similar) is treated as a sensitive domain
automatically: the Autonomous Loop is forced to `semi` (owner approval) and the
answer gets the reasoning and grounding checks. Owners can override in `.env`:
`SENSITIVE_ROLES_EXTRA` forces a role sensitive, and `SENSITIVE_ROLES_REVIEWED_SAFE`
exempts a runtime role that is a false positive. Built-in sensitive roles can never
be exempted. This is a keyword check, not a legal classification, so review any role
that touches regulated work.

### Trying your role

```python
from core.chat import ask
ask("your question", role="pizza_bot")          # a specific role
ask("your question", role="auto")               # the supervisor picks the role
```

Untrusted callers (customer messages) can only be routed to the customer-facing
roles support, sales and marketing. Actions such as webhooks, Slack and email run
only for trusted callers and only through the approval gate in `core/autonomy.py`.

## Building a sensitive-domain AI Employee role

If you're adding a new AI Employee role type that touches a sensitive
domain — health, financial, or legal — add it to `SENSITIVE_DOMAIN_ROLES`
in `core/employees/defaults.py` as part of your PR. This locks the
Autonomous Loop's mode to `semi` or `off` for that role type by default,
regardless of the workspace's general autonomy settings.

Please read the "Vision: Vertical AI Employees" section in
[ROADMAP.md](ROADMAP.md) before starting significant work in this area —
it lays out real open questions (data-handling boundaries, regulatory
scope, escalation guarantees) that should inform the design, not just
the guardrail flag.

## Questions

Open a [Discussion](https://github.com/ragleap/ragleap-core/discussions) or reach out via [ragleap.com](https://ragleap.com).
