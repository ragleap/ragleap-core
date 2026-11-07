# ragleap-security

Small, auditable security primitives for the ragleap libraries. The core is standard library only; encryption is an optional extra. Alpha. **Not independently audited**: see `docs/threat-model.md` for exactly what is and is not defended.

## What is in 0.2.0

From 0.1.0:

- `Scope`: an immutable tenant scope (tenant, subject, agent, session) with an unambiguous key, `covers`, `narrow` and `require_scope`, so stores can refuse to work without one.
- `AuditLog`: an append-only JSON Lines log with a SHA-256 hash chain and `verify()`. Tamper-evident, not tamper-proof (see the threat model).
- `SecretStr`, `from_env`, `redact`: secrets that do not print themselves, read from the environment by name, and a heuristic redactor for logs.
- `check_size`, `check_json_shape`, `bounded_read`, `Deadline`: hard limits for untrusted input.

New in 0.2.0:

- `Key`, `KeyRing`: a 32-byte key with an id that never prints its value, and a ring of keys for rotation.
- `ragleap_security.crypto` (needs `pip install ragleap-security[crypto]`): `encrypt`, `decrypt`, `encrypt_with_passphrase`, `decrypt_with_passphrase`, `reencrypt`, `key_id`. AES-256-GCM through the `cryptography` package, a versioned envelope, and a context (for example a `Scope`) that must match on decrypt.
- `RateLimiter`, `SQLiteRateLimiter`: a token bucket per key, in memory or shared between processes on one machine. They refuse when they cannot decide.
- `build_manifest`, `verify_manifest`, `Manifest`, `file_digest`: detect that files changed, with an optional HMAC so the manifest itself cannot be edited unnoticed.

Nothing in 0.1.0 changed. The new modules are additive.

## Examples

Encrypt data for one tenant and rotate keys:

    from ragleap_security import Key, KeyRing, Scope
    from ragleap_security.crypto import decrypt, encrypt, reencrypt

    scope = Scope("acme", subject="alice")
    old = Key.generate("2026-10")
    box = encrypt(old, b"private notes", scope)           # the scope is authenticated too
    assert decrypt(old, box, scope) == b"private notes"   # a different scope would fail

    new = Key.generate("2026-11")
    ring = KeyRing([new, old])                             # the first key is active
    box = reencrypt(ring, box, scope)                      # now encrypted with "2026-11"

Keys come from the environment by variable name (base64), never from code:

    key = Key.from_env("MY_APP_KEY", "2026-11")

Rate limit by scope:

    from ragleap_security import RateLimiter, Scope

    limiter = RateLimiter(rate=5, burst=10)               # 5 per second, bursts of 10
    limiter.require(Scope("acme", subject="alice"))       # raises RateLimitError when refused

Detect changed files:

    from ragleap_security import Key, build_manifest, verify_manifest

    key = Key.from_env("MY_APP_KEY", "manifest")
    manifest = build_manifest("app/", key=key, exclude=["__pycache__"])
    result = verify_manifest("app/", manifest, key=key, exclude=["__pycache__"])
    assert result.ok, (result.changed, result.missing, result.extra)

## What this does not do

Encryption does not protect data while the program is running, and does not help against someone who has the key. Rate limits do not stop attacks spread over many machines. An integrity check detects changes; it does not prevent anyone from copying your code, models or data. The threat model lists the rest.

## Verifying it yourself

    pip install -e ".[test,crypto]"
    pytest
    python scripts/mutation_check.py   # breaks one protection at a time; every mutation must be killed

Without the `cryptography` package the crypto tests are skipped and everything else still runs.

## Not in 0.2.0

Canary tokens (planned: detect and trace leaks), an SSRF-safe HTTP helper, public-key signatures, streaming encryption for very large files, a policy engine, output detectors and a red-team harness are planned (issue #899) and not available yet.
