# Changelog

All notable changes to `ragleap-security` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.0] - 2026-11-23

Design: #1506. One new module, `ragleap_security.net`; nothing from 0.2.0 changed, so there are no behaviour changes and every 0.2.0 name still works. It is standard library only and is imported on its own (`from ragleap_security.net import ...`).

### Added

- `ragleap_security.net`: `SafeFetcher`, `FetchPolicy`, `FetchResult`, `NetError`, `address_allowed`. Outbound HTTP (GET and HEAD) that is safe by default and per tenant. On every request and every redirect hop it applies strict URL parsing (https only unless the policy allows http; no userinfo, no number-like hosts such as `2130706433` or `0x7f.1`, no IP literals, ASCII only), a host allowlist from the tenant's own policy (deny by default; a missing, wrong-typed or failing policy refuses), one DNS lookup whose EVERY address must pass the address rule, and a connection to the checked address (the name is never resolved twice, so DNS rebinding does not work). The address rule unwraps an embedded IPv4 (IPv4-mapped, NAT64, 6to4) and then allows only global addresses outside an explicit blocklist (verified on Python 3.10.20 and 3.12.3: the `ipaddress` flags for mapped addresses differ between versions, and multicast, NAT64 and IPv4-compatible forms can report `is_global`). Redirects are followed by this module only, with the caller's headers sent to the first origin and never again after leaving it. One monotonic deadline covers connect, TLS and reading; the body is capped and refused (never truncated); `Accept-Encoding: identity` is sent and any content encoding is refused; no proxy or `.netrc` from the environment; TLS verification is always on (system CA store, TLS 1.2 or newer). Every refusal is one fixed reason code. Optional `AuditLog` (`net.request`, `net.redirect`, `net.blocked`: scope, host, port, status, byte count, hop count, reason; never the path, query, headers or body; a failed write raises) and `RateLimiter` (per scope, before any lookup).
- `tests/test_net.py` (258 tests) with a local server; only the DNS answer and the connection target use a documented, underscore-named test seam (loopback is blocked by design).
- `tests/test_hostile_tenant.py` (28 tests; merged earlier, no code change): attacks a hostile tenant would try against scope keys, rate limits, the audit log and secret wrappers.
- 46 more mutations in `scripts/mutation_check.py` for the net module (181 in total, all killed on Python 3.10 and 3.12). The net mutations run against `tests/test_net.py` only, and `python scripts/mutation_check.py net:` runs just them. The first run found 5 survivors: missing tests (the unwrapping of mapped addresses was only caught on Python versions whose flags disagree, the four-tries cap, the deadline between reads and between hops) and one redundant explicit `close()`, which was removed.

### Does not defend

- A host on the allowlist is trusted: a compromised allowed host, or one that redirects to another allowed host, is followed.
- A DNS answer that is consistently wrong (an allowed name pointed at an attacker's public server) is not detected. A slow DNS server can exceed the deadline before the check happens (the lookup cannot be interrupted).
- TLS certificate-authority compromise; local tampering with the hosts file or resolver settings.
- The response is untrusted data: mark steps that fetch as `external` (tainting) in flows and agents.
- Non-HTTP egress; internal services on private names (refused by design, there is no `allow_private`); POST and other methods; IDN names must be given as punycode.
- Windows and macOS are not tested; no independent security review.

### Not verified

Real traffic to the public internet and a real TLS handshake with a valid certificate (the tests use a local plain-HTTP server and check the TLS settings directly); IPv6-only networks; Windows and macOS.

## [0.2.0] - 2026-11-07

Design: #1237. Nothing from 0.1.0 changed: there are no behaviour changes, and every 0.1.0 name still works. The new modules are additive.

### Added

- `Key`, `KeyRing`, `CryptoError` (module `keys`, standard library only): a 32-byte key with an id that never prints its value (no `str`, `repr`, `format`, pickle or hash), `Key.generate`, `Key.from_env` (base64, by variable name), `Key.from_passphrase` (scrypt), `derive` (a separate key per purpose), and a ring of keys with one active key for rotation.
- `ragleap_security.crypto` (optional extra: `pip install ragleap-security[crypto]`, needs `cryptography>=42`): `encrypt`, `decrypt`, `encrypt_with_passphrase`, `decrypt_with_passphrase`, `reencrypt`, `key_id`. AES-256-GCM, a versioned envelope with the key id (or the scrypt settings) in the header, and a `context` (bytes, str or `Scope`) that must match on decrypt. The header and the context are authenticated. Every decrypt failure with a key gives the same error. Data over 64 MiB and contexts over 4096 bytes are refused. Importing `ragleap_security` does not import `crypto`; importing `crypto` without the package gives a clear error.
- `RateLimiter`, `SQLiteRateLimiter`, `Decision`, `RateLimitError`: a token bucket per key (a string or a `Scope`). Memory use is bounded: when the table is full of busy keys, new keys are refused instead of resetting a throttled key. Both limiters refuse when they cannot decide.
- `build_manifest`, `verify_manifest`, `Manifest`, `IntegrityResult`, `IntegrityError`, `file_digest`: SHA-256 manifests of a directory, with changed, missing and extra files reported, symlinks and special files refused, limits on file count and size, and an optional HMAC-SHA256 (with a key derived from a `Key`) so the manifest itself cannot be edited unnoticed.
- `docs/threat-model.md` rows 7 to 10, each with a "Not defended" column. `scripts/mutation_check.py` now has 135 mutations (105 new).

### Changes from the design in #1237

- `Key` and `KeyRing` live in a standard-library-only module, so `integrity` can sign manifests without the `cryptography` package. `crypto` re-uses them.
- The manifest signature uses a key derived from the `Key` (`derive(b"manifest-mac")`), not the raw key bytes.
- Passphrase encryption (`encrypt_with_passphrase`) was added so the scrypt settings and salt are stored in the envelope, as the design said. Settings outside a fixed range, or needing more than 256 MiB, are refused when an envelope is read.

### Verified

- 328 tests pass on Python 3.10 and on Python 3.12 (sandbox, `cryptography` 50.0.2). Without the `cryptography` package 295 pass and the crypto test file is skipped. The key and crypto tests also pass with `cryptography` 42.0.0, the lowest allowed version, on Python 3.10.
- All 135 mutations are killed on Python 3.10 and on Python 3.12.

### Not verified

- The CI job (it now runs the suite twice: without and with the crypto extra) had not run when this was written.
- Windows (the no-symlink open flag is POSIX only) and network filesystems.
- The SQLite limiter under adversarial timing: the thread test is probabilistic and the transaction mode is not mutation-checked.
- The encryption against test vectors from an outside source: the tests check the envelope, tampering and rotation, not AES-GCM itself. That is the `cryptography` package's job and we did not review it.
- The published wheel and sdist had not been installed from PyPI when this was written.
- No independent security review has been done.

## [0.1.0] - 2026-10-23

First version (design: #899). Nothing existed before, so there are no behaviour changes.

### Added

- `Scope`, `ScopeError`, `require_scope`: immutable tenant scopes with an unambiguous key.
- `AuditLog`, `verify`, `VerifyResult`, `AuditError`: a hash-chained, append-only JSONL audit log.
- `SecretStr`, `from_env`, `redact`, `SecretError`: secret handling and heuristic redaction.
- `check_size`, `check_json_shape`, `bounded_read`, `Deadline`, `LimitError`: hard input limits.
- `docs/threat-model.md` and `scripts/mutation_check.py` (30 mutations).

### Verified

- 101 tests pass on Python 3.10 (the VPS) and on Python 3.12 (the author's sandbox). All 30 mutations are killed on Python 3.10 and 3.12.
- The CI test job passes on Python 3.10, 3.11 and 3.12 (PR #907).

### Not verified

- Windows, network filesystems.
- Tenant isolation end to end: no store uses `Scope` yet.
- The cross-process and in-process locks under adversarial timing (the race tests are probabilistic).
- The published wheel and sdist had not been installed from PyPI when this was written (the GitHub Release records that check).
- No independent security review has been done.
