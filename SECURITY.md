# Security Policy

## Supported Versions

Security fixes are made against the **latest published release only**. There
is no long-term-support branch at this stage of the project.

| Component | Supported | Notes |
|---|---|---|
| `ragleap-core` (this repository, GitHub releases and the `ragleap-app` image) | Latest release (currently 0.13.x) | Older releases: please upgrade |
| Python packages in `packages/` (for example `ragleap-rag`, `ragleap-security`) | Latest PyPI release of each package | Older releases: please upgrade |

Published advisories are listed at
[github.com/ragleap/ragleap-core/security/advisories](https://github.com/ragleap/ragleap-core/security/advisories).
For example, GHSA-fg9g-772q-rr9c (voice WebSocket server had no
authentication) affected `ragleap-core` v0.1.0 to v0.12.0 and is fixed in
v0.12.1.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report it privately using one of these methods:

1. **GitHub Security Advisories (preferred):** open a private report via
   [github.com/ragleap/ragleap-core/security/advisories/new](https://github.com/ragleap/ragleap-core/security/advisories/new)
2. **Email:** send details to the address listed on the maintainer's GitHub
   profile ([@antonyrag](https://github.com/antonyrag)).

Please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce, or a minimal proof-of-concept
- The affected component and version(s)

## What to Expect

This is a small, volunteer-based open-source project. There is no dedicated
security team and no formal SLA. Here is the realistic process:

- You will get an acknowledgment as soon as a maintainer sees the report
  (typically within a few days).
- If confirmed, a fix is prioritized over other roadmap work and shipped as a
  patch release. Confirmed issues are published as a GitHub Security Advisory
  with the affected and fixed versions.
- Credit is given to the reporter in the advisory and release notes, unless
  you prefer to stay anonymous: just say so in your report.
- Low-severity issues (for example, requiring an already-compromised API key,
  or affecting only local or development use with no realistic production
  exposure) may be fixed on a slower, non-emergency timeline.

## Scope

This policy covers the code in this repository, including `ragleap-core` and
the packages under `packages/`. It does **not** cover:

- Vulnerabilities in third-party dependencies (please report those upstream;
  flagging them here is still welcome so they can be tracked and patched on
  our side too)
- Deployments run by other people: the configuration, hosting, network
  exposure, keys and logs of an installation are the operator's responsibility
- Private repositories outside this project

## Known Limitations (Honest Disclosure)

In the spirit of this project's "verified claims, not marketing claims"
standard:

- RagLeap has not undergone a formal third-party security audit.
- Approval-gated autonomy and the taint rule reduce risk but do not guarantee
  protection against prompt injection. The Stress Office report in
  `docs/stress-office.md` is a small test with simple injections, not a
  security certificate.
- The `ragleap-rag` guardrail hooks (`input_guardrails` and `output_guardrails`)
  are extension points for user-supplied validators. The library does not
  itself guarantee protection against prompt injection, PII leakage, or
  malicious document content unless you configure guardrails for your use case.
- Container images and packages are not yet signed, and a software bill of
  materials is published only for `ragleap-rag`.
- RagLeap makes no compliance claim (for example CERT-In, SOC 2 or ISO 27001).
  Operators who must meet such requirements should assess their own
  deployment.
