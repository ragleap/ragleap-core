# Changelog

All notable changes to `ragleap-security` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.0] - 2026-11-26

Design: #1569. One new module, `ragleap_security.canary`; nothing from 0.3.0 changed, so there are no behaviour changes and every 0.3.0 name still works. It is standard library only and is imported on its own (`from ragleap_security.canary import ...`). Canaries **detect and trace a leak; they never prevent copying**.

### Added

- `ragleap_security.canary`: `CanaryIssuer`, `Canary`, `CanaryHit`, `Hits`, `CanaryRecord`, `CanaryStore`, `MemoryCanaryStore`, `SQLiteCanaryStore`, `CanaryError`. Tripwire tokens `rlc1_` plus 26 base32 characters (a random 80-bit id and a 48-bit MAC of that id, derived from a `Key` or `KeyRing`). `issue(scope, label)`, `scan(text, scope, source=...)` and `revoke(scope, id)`.
- A canary belongs to exactly one `Scope`; the store record holds it, the token does not, and the token itself is never stored. `scan` returns `own` hits (the canary's scope is inside the scan scope) and `foreign` hits (a valid canary of another scope was found in this scope's data). A foreign hit carries only a count: no id, label or owner. Unknown, revoked and forged tokens are ignored without a trace. Revoking deletes the record; a wrong scope and an unknown id give the same error.
- Lookalikes are rejected by the MAC before any store lookup, so junk that looks like a token cannot push a real one out of the candidate limit. Matching ignores case, fullwidth forms and format characters (zero-width, bidi, soft hyphen).
- Audit events `canary.issued`, `canary.hit`, `canary.foreign`, `canary.revoked` hold scope keys, canary ids, labels and source names; never the scanned text and never a token. If an audit record cannot be written, `issue` leaves no canary behind and `scan` returns nothing (reason `audit_failed`).
- Limits: scanned text 1 MiB by default (ceiling 16 MiB; refused, never truncated), normalised text at most 4 times that many characters, 64 valid tokens looked up per scan (ceiling 256; the list reports `truncated`), 1,000 canaries per scope (ceiling 100,000), labels and source names of 1 to 64 characters. An optional `RateLimiter` keyed by scope limits scans. Every refusal is a fixed reason code on `CanaryError`.
- `tests/test_canary.py` (91 tests; the store-dependent ones run against both the memory and the SQLite store, and the cap is tested under a forced race).
- 70 more mutations in `scripts/mutation_check.py` for the canary module (251 in total, all killed on Python 3.10 and 3.12). `python scripts/mutation_check.py canary:` runs just them against `tests/test_canary.py`. The first run found 1 survivor (the MAC was not shown to belong to its id), which is now tested.

### Differences from the design in #1569

- The MAC covers the id only (the design had the scope in it too). With the scope in the MAC a scan could not reject a lookalike before a store lookup, and 64 junk lookalikes could hide a real token. The scope still binds the canary: it is in the store record, and a token found in another scope's data is a `foreign` hit.
- Revoking deletes the record (the design had a revoked flag). A revoked token is ignored, as designed, and the table stays bounded.

### Does not defend

- Prevention of any kind: the data is already copied when a canary fires. A hit proves the string reached the place you scanned, not who put it there or why. No hit proves nothing: a model may drop or paraphrase a token.
- An attacker who knows the format and filters tokens out (the prefix `rlc1_` is visible).
- Transformed copies: base64 or hex, translation, paraphrase, splitting with a visible character or across messages, typos, screenshots, homoglyphs outside NFKC.
- Compromise of the key (lets an attacker forge tokens: false alarms). Compromise of the store (shows which ids exist, not the tokens).
- No network beacon. Leaks through places you do not scan are missed. The caller passes the scope; nothing here authenticates anyone.
- Windows and macOS are not tested; no independent security review.

### Not verified

A real model provider; Windows and macOS; network file systems for the SQLite store.

### Not in 0.4.0 (deliberately)

Watermarks (invisible marks are removed by normalisation and many copy paths, and a statistical text watermark needs the model), network beacons, and agent integration (a later release of ragleap-agents).

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
