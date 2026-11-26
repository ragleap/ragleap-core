#!/usr/bin/env python3
"""Mutation check for ragleap-security (repository only, not part of the installed package).

For each mutation below it copies the package to a temp directory, breaks one protection in the
copy, and runs the test suite. A mutation is KILLED if a test fails, SURVIVED if every test still
passes (meaning that protection is not really tested). The real source is never modified.

  python scripts/mutation_check.py

Exit code 0 only if the unmodified suite passes and every mutation is killed. Run from the package
directory. Not covered, by design: the cross-process and in-process locks (their race tests are
probabilistic, so a surviving mutant there would prove nothing either way) and the SQLite\ntransaction mode. Needs the cryptography package."""
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
S, A, K, L = "src/ragleap_security/scope.py", "src/ragleap_security/audit.py", "src/ragleap_security/secrets.py", "src/ragleap_security/limits.py"
NT = "src/ragleap_security/net.py"
CY = "src/ragleap_security/canary.py"
KY, CR, RL, IN = ("src/ragleap_security/keys.py", "src/ragleap_security/crypto.py",
                  "src/ragleap_security/ratelimit.py", "src/ragleap_security/integrity.py")

# (name, [(file, old, new), ...]) ; each old string must occur exactly once in its file
MUTATIONS = [
    ("scope: key separator removed", [(S, 'return "/".join(f"{f}=', 'return "".join(f"{f}=')]),
    ("scope: '/' allowed in parts", [(S, "[A-Za-z0-9_.:@-]{0,127}", "[A-Za-z0-9_.:@/-]{0,127}")]),
    ("scope: covers ignores tenant", [(S, "if self.tenant != other.tenant:", "if False:")]),
    ("scope: covers accepts subclasses", [(S, "if type(other) is not Scope:", "if not isinstance(other, Scope):")]),
    ("scope: require_scope accepts subclasses", [(S, "if type(value) is not Scope:", "if not isinstance(value, Scope):")]),
    ("scope: narrow may overwrite a set part", [(S, "if values[name] is not None and values[name] != value:", "if False:")]),
    ("scope: parse accepts a key without tenant", [(S, 'if "tenant" not in values:', "if False:")]),
    ("audit: record hash not checked", [(A, 'if not isinstance(rec["hash"], str) or not hmac.compare_digest(rec["hash"], _digest(body)):', 'if not isinstance(rec["hash"], str):')]),
    ("audit: sequence and chain link not checked", [(A, 'if rec["seq"] != count + 1 or isinstance(rec["seq"], bool):', "if False:"), (A, 'if rec["prev"] != prev:', "if False:")]),
    ("audit: anchor count not checked", [(A, "if expected_count is not None and expected_count != count:", "if False:")]),
    ("audit: anchor hash not checked", [(A, "if expected_last_hash is not None and not hmac.compare_digest(expected_last_hash, prev):", "if False:")]),
    ("audit: append after an incomplete record", [(A, 'if not tail.endswith(b"\\n"):', "if False:")]),
    ("audit: record size cap removed", [(A, "if len(line) > MAX_RECORD_BYTES:", "if False:")]),
    ("audit: SecretStr written in clear", [(A, 'return "[REDACTED]"', "return o.reveal()")]),
    ("audit: log file made world-readable", [(A, "os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600", "os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644")]),
    ("secrets: str() reveals the value", [(K, 'def __str__(self) -> str:\n        return "**********"', "def __str__(self) -> str:\n        return self._v")]),
    ("secrets: format() reveals the value", [(K, 'def __format__(self, spec: str) -> str:\n        return "**********"', "def __format__(self, spec: str) -> str:\n        return self._v")]),
    ("secrets: pickling allowed", [(K, 'raise TypeError("SecretStr cannot be pickled")', "return (SecretStr, (self._v,))")]),
    ("secrets: exact-value redaction off", [(K, "if isinstance(v, str) and len(v) >= MIN_REDACT_LEN:", "if False:")]),
    ("secrets: short values redacted", [(K, "len(v) >= MIN_REDACT_LEN", "len(v) >= 1")]),
    ("secrets: sk- keys not redacted", [(K, '(re.compile(r"\\bsk-[A-Za-z0-9_-]{16,200}"), MASK),', "")]),
    ("secrets: AWS keys not redacted", [(K, '(re.compile(r"\\bAKIA[0-9A-Z]{16}\\b"), MASK),', "")]),
    ("secrets: private keys not redacted", [(K, '.{0,8000}?-----END', ".{0,0}?-----END")]),
    ("secrets: redact length cap removed", [(K, "if len(text) > max_chars:", "if False:")]),
    ("limits: size boundary off by one", [(L, "if n > max_bytes:", "if n >= max_bytes:")]),
    ("limits: size counted in characters", [(L, 'n = len(data.encode("utf-8", "surrogatepass"))', "n = len(data)")]),
    ("limits: bounded_read cannot see excess", [(L, "fp.read(max_bytes + 1)", "fp.read(max_bytes)")]),
    ("limits: depth not checked", [(L, "if depth > max_depth:", "if False:")]),
    ("limits: node count not checked", [(L, "if nodes > max_nodes:", "if False:")]),
    ("limits: deadline never expires at the boundary", [(L, "return self._clock() >= self._end", "return self._clock() > self._end")]),

    # --- keys (0.2.0)
    ("keys: key id check removed", [(KY, "if not isinstance(key_id, str) or not _KEY_ID.match(key_id):", "if not isinstance(key_id, str):")]),
    ("keys: key length not checked", [(KY, "if not isinstance(raw, (bytes, bytearray)) or len(raw) != KEY_BYTES:", "if not isinstance(raw, (bytes, bytearray)):")]),
    ("keys: repr and str reveal the key", [(KY, 'return f"Key(key_id={self.key_id!r})"', 'return f"Key(key_id={self.key_id!r}, {self.reveal_b64()})"')]),
    ("keys: pickling allowed", [(KY, 'raise TypeError("Key cannot be pickled")', "return (Key, (self._k, self.key_id))")]),
    ("keys: key made hashable", [(KY, "__hash__ = None  # type: ignore[assignment]", "__hash__ = object.__hash__")]),
    ("keys: equality ignores the key bytes", [(KY, "return self.key_id == other.key_id and hmac.compare_digest(self._k, other._k)", "return self.key_id == other.key_id")]),
    ("keys: derive not bound to its purpose", [(KY, 'b"ragleap-security/derive/v1/" + bytes(purpose)', 'b"ragleap-security/derive/v1/"')]),
    ("keys: derive returns the raw key", [(KY, 'return hmac.new(self._k, b"ragleap-security/derive/v1/" + bytes(purpose), hashlib.sha256).digest()', "return self._k")]),
    ("keys: passphrase floor removed", [(KY, "len(pw) < MIN_PASSPHRASE_CHARS", "len(pw) < 0")]),
    ("keys: passphrase floor off by one", [(KY, "len(pw) < MIN_PASSPHRASE_CHARS", "len(pw) <= MIN_PASSPHRASE_CHARS")]),
    ("keys: salt floor removed", [(KY, "len(salt) < MIN_SALT_BYTES", "len(salt) < 0")]),
    ("keys: salt floor off by one", [(KY, "len(salt) < MIN_SALT_BYTES", "len(salt) <= MIN_SALT_BYTES")]),
    ("keys: scrypt memory cap removed", [(KY, "if 128 * (1 << log2_n) * r > _SCRYPT_MAX_MEMORY:", "if False:")]),
    ("keys: scrypt memory cap off by one", [(KY, "if 128 * (1 << log2_n) * r > _SCRYPT_MAX_MEMORY:", "if 128 * (1 << log2_n) * r >= _SCRYPT_MAX_MEMORY:")]),
    ("keys: scrypt ranges removed", [(KY, "if not 14 <= log2_n <= 18 or not 1 <= r <= 16 or not 1 <= p <= 4:", "if False:")]),
    ("keys: scrypt log2_n lower bound lowered", [(KY, "not 14 <= log2_n <= 18", "not 13 <= log2_n <= 18")]),
    ("keys: scrypt log2_n upper bound raised", [(KY, "not 14 <= log2_n <= 18", "not 14 <= log2_n <= 19")]),
    ("keys: scrypt p upper bound raised", [(KY, "not 1 <= p <= 4", "not 1 <= p <= 5")]),
    ("keys: scrypt r lower bound lowered", [(KY, "not 1 <= r <= 16", "not 0 <= r <= 16")]),
    ("keys: bool accepted as scrypt setting", [(KY, "if isinstance(v, bool) or not isinstance(v, int):", "if not isinstance(v, int):")]),
    ("keys: duplicate key ids allowed in a ring", [(KY, "if k.key_id in found:", "if False:")]),
    ("keys: ring accepts Key subclasses", [(KY, "if type(k) is not Key:", "if not isinstance(k, Key):")]),
    ("keys: active key id not checked", [(KY, "if active not in found:", "if False:")]),
    # --- crypto (0.2.0)
    ("crypto: header not authenticated", [(CR, "return header + struct.pack(\">I\", len(context)) + context", "return struct.pack(\">I\", len(context)) + context")]),
    ("crypto: context not authenticated", [(CR, "return header + struct.pack(\">I\", len(context)) + context", "return header")]),
    ("crypto: nonce is constant (encrypt)", [(CR, 'kid = k.key_id.encode("ascii")\n    nonce = os.urandom(NONCE_BYTES)', 'kid = k.key_id.encode("ascii")\n    nonce = bytes(NONCE_BYTES)')]),
    ("crypto: nonce is constant (passphrase)", [(CR, "material = derive_scrypt(passphrase, salt, log2_n, r, p)\n    nonce = os.urandom(NONCE_BYTES)", "material = derive_scrypt(passphrase, salt, log2_n, r, p)\n    nonce = bytes(NONCE_BYTES)")]),
    ("crypto: salt is constant", [(CR, "salt = os.urandom(SALT_BYTES)", "salt = bytes(SALT_BYTES)")]),
    ("crypto: data size limit removed (encrypt)", [(CR, 'raise TypeError("key must be a Key or a KeyRing")\n    plain = _bytes(data, "data", MAX_PLAINTEXT_BYTES)', 'raise TypeError("key must be a Key or a KeyRing")\n    plain = _bytes(data, "data", 10 ** 12)')]),
    ("crypto: data size limit removed (passphrase)", [(CR, '"""\n    plain = _bytes(data, "data", MAX_PLAINTEXT_BYTES)', '"""\n    plain = _bytes(data, "data", 10 ** 12)')]),
    ("crypto: context size limit removed", [(CR, 'check_size(context, MAX_CONTEXT_BYTES, "context")', "pass")]),
    ("crypto: context size limit off by one", [(CR, "MAX_CONTEXT_BYTES = 4096", "MAX_CONTEXT_BYTES = 4095")]),
    ("crypto: envelope size limit removed", [(CR, 'raw = _bytes(envelope, "envelope", MAX_PLAINTEXT_BYTES + 1024)', 'raw = _bytes(envelope, "envelope", 10 ** 12)')]),
    ("crypto: magic not checked", [(CR, "raw[:4] != MAGIC", "False")]),
    ("crypto: version not checked", [(CR, "raw[4] != VERSION", "False")]),
    ("crypto: algorithm not checked", [(CR, "raw[5] != ALG_AES256GCM", "False")]),
    ("crypto: key id length not checked", [(CR, "if not 1 <= n <= 64 or len(info) != n:", "if False:")]),
    ("crypto: key id not required to be ASCII", [(CR, 'info = info.decode("ascii")', 'info = info.decode("latin-1")')]),
    ("crypto: unknown kdf accepted", [(CR, "    else:\n        raise CryptoError(_FORMAT)\n    nonce = raw[pos:pos + NONCE_BYTES]", "    else:\n        info = None\n    nonce = raw[pos:pos + NONCE_BYTES]")]),
    ("crypto: key id not matched to the key", [(CR, "k = keys if keys.key_id == info else None", "k = keys")]),
    ("crypto: decrypt accepts a passphrase envelope", [(CR, "if kdf != KDF_NONE:", "if False:")]),
    ("crypto: decrypt_with_passphrase accepts a key envelope", [(CR, "if kdf != KDF_SCRYPT:", "if False:")]),
    ("crypto: encrypt ignores the active key", [(CR, "k = key.active if type(key) is KeyRing else key", "k = next(iter(key._keys.values())) if type(key) is KeyRing else key")]),
    ("crypto: Scope not accepted as context", [(CR, "    if type(context) is Scope:\n        context = context.key\n", "")]),
    ("crypto: reencrypt does not rotate", [(CR, "return encrypt(ring, decrypt(ring, envelope, context), context)", "return envelope")]),
    ("crypto: reencrypt drops the context", [(CR, "return encrypt(ring, decrypt(ring, envelope, context), context)", "return encrypt(ring, decrypt(ring, envelope, context))")]),
    ("crypto: a wrong tag gives a different error", [(CR, "raise CryptoError(_GENERIC) from None", 'raise CryptoError("authentication failed") from None')]),
    # --- ratelimit (0.2.0)
    ("ratelimit: memory refill not capped at burst", [(RL, "tokens = min(self.burst, bucket[0] + max(0.0, now - bucket[1]) * self.rate)", "tokens = bucket[0] + max(0.0, now - bucket[1]) * self.rate")]),
    ("ratelimit: sqlite refill not capped at burst", [(RL, "tokens = min(self.burst, row[0] + max(0.0, now - row[1]) * self.rate)", "tokens = row[0] + max(0.0, now - row[1]) * self.rate")]),
    ("ratelimit: memory clock going back gives tokens", [(RL, "bucket[0] + max(0.0, now - bucket[1]) * self.rate)", "bucket[0] + (now - bucket[1]) * self.rate)")]),
    ("ratelimit: sqlite clock going back gives tokens", [(RL, "row[0] + max(0.0, now - row[1]) * self.rate)", "row[0] + (now - row[1]) * self.rate)")]),
    ('ratelimit: memory exact cost refused', [(RL, '            if c > tokens:\n                self._buckets[k] = [tokens, now]', '            if c >= tokens:\n                self._buckets[k] = [tokens, now]')]),
    ('ratelimit: sqlite exact cost refused', [(RL, '            if c > tokens:\n                decision = self._deny(tokens, c)', '            if c >= tokens:\n                decision = self._deny(tokens, c)')]),
    ('ratelimit: memory cost not spent', [(RL, '            tokens -= c\n            self._buckets[k] = [tokens, now]', '            tokens -= 0\n            self._buckets[k] = [tokens, now]')]),
    ('ratelimit: sqlite cost not spent', [(RL, '                tokens -= c\n                decision = Decision(True, tokens, 0.0)', '                tokens -= 0\n                decision = Decision(True, tokens, 0.0)')]),
    ('ratelimit: memory key count not limited', [(RL, 'if len(self._buckets) >= self.max_keys and not self._evict(now):', 'if False:')]),
    ("ratelimit: memory forgets busy keys", [(RL, "if tokens + max(0.0, now - last) * self.rate >= self.burst:", "if True:")]),
    ("ratelimit: memory forgets only strictly full keys", [(RL, "if tokens + max(0.0, now - last) * self.rate >= self.burst:", "if tokens + max(0.0, now - last) * self.rate > self.burst:")]),
    ("ratelimit: eviction scan is longer than 32", [(RL, "_EVICT_SCAN = 32", "_EVICT_SCAN = 64")]),
    ("ratelimit: memory scan examines one extra key", [(RL, "if i >= _EVICT_SCAN:", "if i > _EVICT_SCAN:")]),
    ('ratelimit: sqlite key count not limited', [(RL, '                if count >= self.max_keys:\n                    conn.execute(\n                        "DELETE FROM', '                if False:\n                    conn.execute(\n                        "DELETE FROM')]),
    ('ratelimit: sqlite accepts a new key when still full', [(RL, '                    if count >= self.max_keys:\n                        conn.execute("COMMIT")', '                    if False:\n                        conn.execute("COMMIT")')]),
    ("ratelimit: sqlite forgets busy keys", [(RL, "* ? >= ?)", "* ? >= ? - 1000000)")]),
    ("ratelimit: sqlite forgets only strictly full keys", [(RL, "* ? >= ?)", "* ? > ?)")]),
    ("ratelimit: sqlite scan examines one extra key", [(RL, "(_EVICT_SCAN, now, self.rate, self.burst))", "(_EVICT_SCAN + 1, now, self.rate, self.burst))")]),
    ("ratelimit: key length cap removed", [(RL, "1 <= len(key) <= MAX_KEY_CHARS", "1 <= len(key) <= 10 ** 6")]),
    ("ratelimit: empty key accepted", [(RL, "1 <= len(key) <= MAX_KEY_CHARS", "0 <= len(key) <= MAX_KEY_CHARS")]),
    ('ratelimit: zero cost accepted', [(RL, 'return _positive(cost, "cost", 1e-9)', 'return _positive(cost, "cost", 0.0)')]),
    ('ratelimit: retry_after ignores the rate', [(RL, 'else (c - tokens) / self.rate', 'else (c - tokens)')]),
    ('ratelimit: cost above burst has a finite retry_after', [(RL, 'retry = math.inf if c > self.burst else', 'retry = 5.0 if c > self.burst else')]),
    ("ratelimit: sqlite file made world-readable", [(RL, "os.O_RDWR | os.O_CREAT, 0o600", "os.O_RDWR | os.O_CREAT, 0o644")]),
    ("ratelimit: sqlite failure allows the request", [(RL, 'return Decision(False, 0.0, 1.0, "unavailable")', 'return Decision(True, 0.0, 0.0, "")')]),
    ("ratelimit: require does not raise", [(RL, "        if not d.allowed:\n            raise RateLimitError", "        if False:\n            raise RateLimitError")]),
    # --- integrity (0.2.0)
    ("integrity: file size limit removed", [(IN, "if total > max_bytes:", "if False:")]),
    ("integrity: file size limit off by one", [(IN, "if total > max_bytes:", "if total >= max_bytes:")]),
    ("integrity: symlinked file followed", [(IN, 'os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | ', "os.O_RDONLY | ")]),
    ("integrity: non-regular file hashed", [(IN, "if not stat.S_ISREG(os.fstat(fd).st_mode):", "if False:")]),
    ("integrity: symlinked directory followed", [(IN, "            if os.path.islink(full):\n", "            if False:\n")]),
    ('integrity: excluded directory not skipped', [(IN, '            if name in skip:\n                continue\n            full = os.path.join(dirpath, name)\n            if os.path.islink(full):', '            if False:\n                continue\n            full = os.path.join(dirpath, name)\n            if os.path.islink(full):')]),
    ('integrity: excluded file not skipped', [(IN, '            if name in skip:\n                continue\n            full = os.path.join(dirpath, name)\n            rel =', '            if False:\n                continue\n            full = os.path.join(dirpath, name)\n            rel =')]),
    ("integrity: file count not limited", [(IN, "if len(regular) + len(special) > max_files:", "if False:")]),
    ("integrity: file count off by one", [(IN, "if len(regular) + len(special) > max_files:", "if len(regular) + len(special) >= max_files:")]),
    ("integrity: root not checked", [(IN, "if not os.path.isdir(root):", "if False:")]),
    ("integrity: build accepts symlinks", [(IN, "    if special:\n        raise IntegrityError(\"refusing", "    if False:\n        raise IntegrityError(\"refusing")]),
    ("integrity: NUL allowed in a manifest path", [(IN, 'or "\\x00" in path', "")]),
    ('integrity: .. allowed in a manifest path', [(IN, 'part in ("", ".", "..")', 'part in ("", ".")')]),
    ('integrity: . allowed in a manifest path', [(IN, 'part in ("", ".", "..")', 'part in ("", "..")')]),
    ('integrity: empty path part allowed', [(IN, 'part in ("", ".", "..")', 'part in (".", "..")')]),
    ("integrity: digest may be upper case", [(IN, '_HEX64 = re.compile(r"[0-9a-f]{64}\\Z")', '_HEX64 = re.compile(r"[0-9a-fA-F]{64}\\Z")')]),
    ("integrity: digest length not exact", [(IN, '_HEX64 = re.compile(r"[0-9a-f]{64}\\Z")', '_HEX64 = re.compile(r"[0-9a-f]{64}")')]),
    ("integrity: manifest fields not checked", [(IN, 'set(data) != {"format", "version", "files", "mac"}', "False")]),
    ('integrity: bool accepted as manifest version', [(IN, ' or isinstance(data["version"], bool)', '')]),
    ('integrity: manifest file count not limited', [(IN, 'if len(files) > DEFAULT_MAX_FILES:', 'if False:')]),
    ('integrity: manifest file count off by one', [(IN, 'if len(files) > DEFAULT_MAX_FILES:', 'if len(files) >= DEFAULT_MAX_FILES:')]),
    ("integrity: manifest size not limited", [(IN, 'check_size(text, MAX_MANIFEST_BYTES, "manifest")', "pass")]),
    ("integrity: signature not checked", [(IN, "if not hmac.compare_digest(manifest.mac, _mac(manifest.files, key)):", "if False:")]),
    ("integrity: unsigned manifest accepted when a key is given", [(IN, "if manifest.mac is None:\n            return", "if False:\n            return")]),
    ('integrity: signature uses the raw key', [(IN, 'hmac.new(key.derive(b"manifest-mac"), _canonical(files), hashlib.sha256)', 'hmac.new(key._material(), _canonical(files), hashlib.sha256)')]),
    ('integrity: changed file not detected', [(IN, '        if not same:\n            changed.append(rel)', '        if False:\n            changed.append(rel)')]),
    ("integrity: extra file not detected", [(IN, "            extra.append(rel)\n            continue", "            continue")]),
    ('integrity: missing file not detected', [(IN, 'if rel not in regular and rel not in special]', 'if False]')]),
    ('integrity: replaced file also reported missing', [(IN, 'if rel not in regular and rel not in special]', 'if rel not in regular]')]),
    ("integrity: extra files do not fail verification", [(IN, "ok = not (changed or missing or extra)", "ok = not (changed or missing)")]),
    ('integrity: listed symlink reported as extra', [(IN, '(changed if rel in listed else extra).append(rel)', 'extra.append(rel)')]),
    ('integrity: signature check not reported', [(IN, 'tuple(sorted(extra)), mac_checked)', 'tuple(sorted(extra)), False)')]),
    # ---- net (0.3.0, design #1506)
    ("net: mapped IPv4 is not unwrapped", [(NT, "    if mapped is not None:\n        return _v4_ok(mapped)\n", "    if False:\n        return _v4_ok(mapped)\n")]),
    ("net: NAT64 is not unwrapped", [(NT, "    if ip in _NAT64:", "    if False:")]),
    ("net: 6to4 is not unwrapped", [(NT, "    if ip in _SIX_TO_FOUR:", "    if False:")]),
    ("net: IPv6 special ranges are not blocked", [(NT, "    if any(ip in net for net in _V6_BLOCK):\n        return False\n", "")]),
    ("net: IPv4 multicast is allowed", [(NT, "ip.is_global and not ip.is_multicast and not any(ip in n for n in _V4_BLOCK)", "ip.is_global and not any(ip in n for n in _V4_BLOCK)"), (NT, '"224.0.0.0/4", "240.0.0.0/4")]', '"240.0.0.0/4")]')]),
    ("net: IPv6 multicast is allowed", [(NT, "    return ip.is_global and not ip.is_multicast\n", "    return ip.is_global\n"), (NT, '"fec0::/10", "ff00::/8")]', '"fec0::/10")]')]),
    ("net: IPv6 zone ids are accepted", [(NT, 'if not isinstance(text, str) or "%" in text:', "if not isinstance(text, str):")]),
    ("net: userinfo is accepted", [(NT, 'if "@" in authority:', "if False:")]),
    ("net: number-like hosts are accepted", [(NT, 'if last.isdigit() or last.startswith("0x"):\n        raise NetError("ip_literal")', 'if False:\n        raise NetError("ip_literal")')]),
    ("net: IP literals in brackets are accepted", [(NT, 'if authority.startswith("["):', "if False:")]),
    ("net: wildcard matches without a dot", [(NT, "if host.endswith(suffix) and len(host) > len(suffix):", "if host.endswith(suffix[1:]) and len(host) > len(suffix):")]),
    ("net: the port is not checked", [(NT, "if port not in policy.allow_ports:", "if False:")]),
    ("net: http without the policy switch", [(NT, '(scheme == "http" and not policy.allow_http)', "False")]),
    ("net: any scheme is accepted", [(NT, 'if scheme not in ("https", "http") or', "if False or")]),
    ("net: non-ASCII or control characters in the URL", [(NT, "if any(not 0x21 <= ord(c) <= 0x7E for c in url) or", "if False or")]),
    ("net: fragments and backslashes in the URL", [(NT, '"\\\\" in url or "#" in url:', "False:")]),
    ("net: the host allowlist is not applied", [(NT, "if not _host_allowed(host, policy.allow_hosts):", "if False:")]),
    ("net: policy accepts IP entries", [(NT, '            if last.isdigit() or last.startswith("0x"):\n                raise ValueError("allow_hosts takes host names, not IP addresses")\n', "")]),
    ("net: policy accepts a one-label wildcard", [(NT, '_PATTERN = re.compile(r"(\\*\\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\\Z")', '_PATTERN = re.compile(r"(\\*\\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\\Z")')]),
    ("net: one blocked address among several is tolerated", [(NT, "if not all(self._address_rule(a) for a in found):", "if not any(self._address_rule(a) for a in found):")]),
    ("net: too many addresses are accepted", [(NT, "if not found or len(found) > MAX_ADDRESSES:", "if not found:")]),
    ("net: connects to the name instead of the checked address", [(NT, "sock = self._connector(ip, t.port, max(0.001, deadline.remaining()))", "sock = self._connector(t.host, t.port, max(0.001, deadline.remaining()))")]),
    ("net: tries more than four addresses", [(NT, "for ip in found[:MAX_TRIES]:", "for ip in found:")]),
    ("net: the redirect cap is off by one", [(NT, "if hops >= policy.max_redirects:", "if hops > policy.max_redirects:")]),
    ("net: caller headers follow a cross-origin redirect", [(NT, "                elif origin != first:\n                    left_first_origin = True", "                elif origin != first:\n                    left_first_origin = False")]),
    ("net: the redirect location is not checked", [(NT, '"\\\\" in location:\n            raise NetError("bad_redirect", host)', 'False:\n            raise NetError("bad_redirect", host)')]),
    ("net: certificates are not verified", [(NT, "ctx = ssl.create_default_context()", "ctx = ssl._create_unverified_context()")]),
    ("net: old TLS versions are accepted", [(NT, "ctx.minimum_version = ssl.TLSVersion.TLSv1_2\n", "")]),
    ("net: content encoding is accepted", [(NT, 'if enc is not None and enc.strip().lower() not in ("", "identity"):', "if False:")]),
    ("net: the body cap is not enforced while reading", [(NT, "            if total > policy.max_bytes:\n                raise NetError(\"response_too_large\", t.host)\n            chunks.append(chunk)", "            chunks.append(chunk)")]),
    ("net: the deadline is not checked while reading", [(NT, "            if remaining <= 0:\n                raise NetError(\"timeout\", t.host)\n            sock.settimeout(remaining)", "            sock.settimeout(max(remaining, 0.001))")]),
    ("net: the response header size is not capped", [(NT, "if sum(len(k) + len(v) + 4 for k, v in pairs) > policy.max_header_bytes:", "if False:")]),
    ("net: forbidden caller headers are accepted", [(NT, "or k.lower() in _FORBIDDEN_HEADERS", "")]),
    ("net: caller header values are not checked", [(NT, "or any(not 0x20 <= ord(c) <= 0x7E for c in v)):", "):")]),
    ("net: too many caller headers", [(NT, "or len(headers) > MAX_HEADERS_FROM_CALLER:", ":")]),
    ("net: a policy subclass is trusted", [(NT, "if type(policy) is not FetchPolicy:", "if not isinstance(policy, FetchPolicy):")]),
    ("net: an exception in policy_for escapes", [(NT, 'raise NetError("bad_policy") from None\n        if policy is None:', 'raise\n        if policy is None:')]),
    ("net: no policy still fetches", [(NT, 'if policy is None:\n            raise NetError("no_policy")', 'if False:\n            raise NetError("no_policy")')]),
    ("net: the rate limit is ignored", [(NT, "        if not decision.allowed:\n            raise RateLimitError", "        if False:\n            raise RateLimitError")]),
    ("net: the scope is not required", [(NT, "        scope = require_scope(scope)\n        self._limit(scope)", "        self._limit(scope)")]),
    ("net: the audit record holds the URL", [(NT, 'self._record(scope, "net.request", host=host, port=port, status=status, bytes=len(body), hops=hops)', 'self._record(scope, "net.request", host=host, port=port, status=status, bytes=len(body), hops=hops, url=current)')]),
    ("net: refusals are not audited", [(NT, '            self._record(scope, "net.blocked", reason=e.reason, host=e.host or host or "", hops=hops)\n', "")]),
    ("net: redirects are not audited", [(NT, '                    self._record(scope, "net.redirect", host=host, port=port, hops=hops)\n', "                    pass\n")]),
    ("net: the connection is not closed", [(NT, "            resp.close()\n            conn.close()\n", "            resp.close()\n")]),
    ("net: the deadline is not checked before a hop", [(NT, "        if deadline.expired():\n            raise NetError(\"timeout\", t.host)\n        try:\n            found", "        try:\n            found")]),
    ("net: the status of a redirect without Location is followed", [(NT, 'if status in _REDIRECTS and resp_headers.get("location"):', "if status in _REDIRECTS:")]),
    # ---- canary (0.4.0, design #1569)
    ("canary: token pattern is one character short", [(CY, '_TOKEN = re.compile(r"rlc1_[a-z2-7]{26}")', '_TOKEN = re.compile(r"rlc1_[a-z2-7]{25}")')]),
    ("canary: non-canonical spelling accepted", [(CY, "if len(raw) != ID_BYTES + MAC_BYTES or _encode(raw) != token:", "if len(raw) != ID_BYTES + MAC_BYTES:")]),
    ("canary: MAC does not cover the id", [(CY, 'b"rlc1" + canary_id, hashlib.sha256', 'b"rlc1", hashlib.sha256')]),
    ("canary: MAC not checked", [(CY, "if raw is None or not self._mac_valid(raw[:ID_BYTES], raw[ID_BYTES:]):", "if raw is None:")]),
    ("canary: any MAC is valid", [(CY, "        return ok\n", "        return True\n")]),
    ("canary: only the last ring key counts", [(CY, "ok |= hmac.compare_digest(self._mac(key, canary_id), mac)", "ok = hmac.compare_digest(self._mac(key, canary_id), mac)")]),
    ("canary: only the first ring key is tried", [(CY, "return [self._keys.get(k) for k in self._keys.key_ids]", "return [self._keys.get(self._keys.key_ids[0])]")]),
    ("canary: issue uses the wrong ring key", [(CY, "return self._keys if type(self._keys) is Key else self._keys.active", "return self._keys if type(self._keys) is Key else self._keys.get(self._keys.key_ids[-1])")]),
    ("canary: random id is constant", [(CY, "raw_id = os.urandom(ID_BYTES)", "raw_id = b'\\x00' * ID_BYTES")]),
    ("canary: label not checked on issue", [(CY, "        _check_label(label)\n        key = self._active()", "        key = self._active()")]),
    ("canary: label length limit off by one", [(CY, "[A-Za-z0-9_.:@-]{0,63}\\Z\")\n_ID_HEX", "[A-Za-z0-9_.:@-]{0,64}\\Z\")\n_ID_HEX")]),
    ("canary: label type not checked", [(CY, "if not isinstance(value, str) or not _LABEL.match(value):", "if not isinstance(value, str):")]),
    ("canary: source not checked on scan", [(CY, "        _check_label(source)\n", "")]),
    ("canary: record keeps the wrong scope key", [(CY, "CanaryRecord(canary_id, scope.key, label, key.key_id, created)", "CanaryRecord(canary_id, scope.tenant, label, key.key_id, created)")]),
    ("canary: per-scope cap not enforced", [(CY, "if not self._store.add(record, self.max_per_scope):", "if False:")]),
    ("canary: cap error has another reason", [(CY, 'raise CanaryError("limit")', 'raise CanaryError("store_error")')]),
    ("canary: canary kept when its audit write fails", [(CY, "self._store.remove(canary_id, scope.key)  # no canary without its audit record", "pass")]),
    ("canary: issue store error not wrapped", [(CY, "        except Exception:\n            raise CanaryError(\"store_error\") from None\n        try:\n            self._record(\"canary.issued\"", "        except ZeroDivisionError:\n            raise CanaryError(\"store_error\") from None\n        try:\n            self._record(\"canary.issued\"")]),
    ("canary: text type not checked", [(CY, "        if not isinstance(text, str):\n            raise CanaryError(\"invalid_argument\")\n        _check_label(source)", "        if False:\n            raise CanaryError(\"invalid_argument\")\n        _check_label(source)")]),
    ("canary: size limit off by one", [(CY, 'surrogatepass")) > self.max_text_bytes:', 'surrogatepass")) >= self.max_text_bytes:')]),
    ("canary: size counted in characters", [(CY, 'if len(text.encode("utf-8", "surrogatepass")) > self.max_text_bytes:', "if len(text) > self.max_text_bytes:")]),
    ("canary: rate limit ignored", [(CY, 'if not getattr(decision, "allowed", False):', "if False:")]),
    ("canary: text not normalised", [(CY, "normalised = _normalise(text)", "normalised = text")]),
    ("canary: NFKC removed", [(CY, 'folded = unicodedata.normalize("NFKC", text)', "folded = text")]),
    ("canary: format characters kept", [(CY, '!= "Cf"', '== "Cf"')]),
    ("canary: case not folded", [(CY, ").casefold()", ")")]),
    ("canary: expansion cap removed", [(CY, "if len(normalised) > _NORMALIZED_EXPANSION * self.max_text_bytes:", "if False:")]),
    ("canary: repeated tokens counted once", [(CY, "counts[token] = counts.get(token, 0) + 1", "counts[token] = 1")]),
    ("canary: valid tokens not counted against the cap", [(CY, "                valid += 1\n", "")]),
    ("canary: cap off by one", [(CY, "if valid >= self.max_candidates:", "if valid > self.max_candidates:")]),
    ("canary: truncation not flagged", [(CY, "                    truncated = True\n", "")]),
    ("canary: unknown id not skipped", [(CY, "                if record is None:\n                    continue", "                if False:\n                    continue")]),
    ("canary: broken scope key is fatal", [(CY, "                except ScopeError:\n                    continue", "                except ZeroDivisionError:\n                    continue")]),
    ("canary: every valid token is own", [(CY, "if scope.covers(owner):", "if True:")]),
    ("canary: ownership by equality", [(CY, "if scope.covers(owner):", "if scope == owner:")]),
    ("canary: ownership reversed", [(CY, "if scope.covers(owner):", "if owner.covers(scope):")]),
    ("canary: own count wrong", [(CY, "entry[1] += n", "entry[1] += 1")]),
    ("canary: foreign count wrong", [(CY, "foreign_count += n", "foreign_count += 1")]),
    ("canary: owners listed twice", [(CY, "if record.scope_key not in foreign_owners and len(foreign_owners) < MAX_OWNERS_IN_AUDIT:", "if len(foreign_owners) < MAX_OWNERS_IN_AUDIT:")]),
    ("canary: owner list not capped", [(CY, "if record.scope_key not in foreign_owners and len(foreign_owners) < MAX_OWNERS_IN_AUDIT:", "if record.scope_key not in foreign_owners:")]),
    ("canary: id list not capped", [(CY, "and len(foreign_ids) < MAX_OWNERS_IN_AUDIT:", ":")]),
    ("canary: foreign hit always reported", [(CY, "        if foreign_count:\n            hits.append(", "        if True:\n            hits.append(")]),
    ("canary: foreign hit shows the owner", [(CY, 'hits.append(CanaryHit("foreign", foreign_count, None, None, source))', 'hits.append(CanaryHit("foreign", foreign_count, foreign_ids[0], foreign_owners[0], source))')]),
    ("canary: own hit loses its label", [(CY, 'CanaryHit("own", n, record.id, record.label, source)', 'CanaryHit("own", n, record.id, None, source)')]),
    ("canary: truncation flag dropped", [(CY, "        hits.truncated = truncated\n", "")]),
    ("canary: hit audit event not written", [(CY, '            self._record("canary.hit", scope, {"canary": record.id, "label": record.label, "source": source, "count": n})', "            pass")]),
    ("canary: foreign audit event not written", [(CY, '            self._record("canary.foreign", scope, {"owners": foreign_owners, "canaries": foreign_ids,\n                                                   "source": source, "count": foreign_count})', "            pass")]),
    ("canary: audit failure not turned into an error", [(CY, "        except Exception:  # AuditError, or anything the log raises: fail closed", "        except ZeroDivisionError:  # AuditError, or anything the log raises: fail closed")]),
    ("canary: scan store error not wrapped", [(CY, "        except CanaryError:\n            raise\n        except Exception:\n            raise CanaryError(\"store_error\") from None\n\n        hits = Hits()", "        except CanaryError:\n            raise\n        except ZeroDivisionError:\n            raise CanaryError(\"store_error\") from None\n\n        hits = Hits()")]),
    ("canary: revoke does not check the id shape", [(CY, "if not isinstance(canary_id, str) or not _ID_HEX.match(canary_id):", "if False:")]),
    ("canary: revoke ignores a wrong scope", [(CY, "removed = self._store.remove(canary_id, scope.key)", "removed = self._store.remove(canary_id, self._store.get(canary_id).scope_key if self._store.get(canary_id) else scope.key)")]),
    ("canary: revoke of an unknown id succeeds", [(CY, "        if not removed:\n            raise CanaryError(\"no_such_canary\")", "        if False:\n            raise CanaryError(\"no_such_canary\")")]),
    ("canary: revoke not audited", [(CY, '        self._record("canary.revoked", scope, {"canary": canary_id})', "        pass")]),
    ("canary: keys type not checked", [(CY, "if type(keys) not in (Key, KeyRing):", "if False:")]),
    ("canary: store type not checked", [(CY, "if not isinstance(store, CanaryStore):", "if False:")]),
    ("canary: audit type not checked", [(CY, "if audit is not None and not isinstance(audit, AuditLog):", "if False:")]),
    ("canary: rate limiter type not checked", [(CY, 'if rate_limiter is not None and not callable(getattr(rate_limiter, "check", None)):', "if False:")]),
    ("canary: ceiling not checked", [(CY, "or not 1 <= value <= ceiling:", "or not 1 <= value:")]),
    ("canary: bool accepted as a limit", [(CY, "if isinstance(value, bool) or not isinstance(value, int) or not 1 <=", "if not isinstance(value, int) or not 1 <=")]),
    ("canary: default text limit changed", [(CY, "DEFAULT_MAX_TEXT_BYTES = 1 << 20", "DEFAULT_MAX_TEXT_BYTES = 1 << 21")]),
    ("canary: memory store cap off by one", [(CY, "if self._per_scope.get(record.scope_key, 0) >= limit or record.id in self._rows:", "if self._per_scope.get(record.scope_key, 0) > limit or record.id in self._rows:")]),
    ("canary: memory store accepts a duplicate id", [(CY, "or record.id in self._rows:", ":")]),
    ("canary: memory store removes another scope's record", [(CY, "if rec is None or rec.scope_key != scope_key:", "if rec is None:")]),
    ("canary: memory store does not free room on remove", [(CY, "            self._per_scope[scope_key] -= 1\n", "")]),
    ("canary: sqlite store cap off by one", [(CY, "if n >= limit or conn.execute(", "if n > limit or conn.execute(")]),
    ("canary: sqlite store accepts a duplicate id", [(CY, 'or conn.execute("SELECT 1 FROM canaries WHERE id = ?", (record.id,)).fetchone():', ":")]),
    ("canary: sqlite store removes another scope's record", [(CY, '"DELETE FROM canaries WHERE id = ? AND scope_key = ?", (canary_id, scope_key))', '"DELETE FROM canaries WHERE id = ? AND ? = ?", (canary_id, scope_key, scope_key))')]),
    ("canary: sqlite remove always reports success", [(CY, "return cur.rowcount == 1", "return True")]),
    ("canary: sqlite count ignores the scope", [(CY, 'conn.execute("SELECT COUNT(*) FROM canaries WHERE scope_key = ?", (scope_key,)).fetchone()[0]\n        finally:', 'conn.execute("SELECT COUNT(*) FROM canaries").fetchone()[0]\n        finally:')]),
    ("canary: sqlite file readable by others", [(CY, "os.O_RDWR | os.O_CREAT, 0o600", "os.O_RDWR | os.O_CREAT, 0o644")]),
]


TEST_TIMEOUT_SECONDS = 120
WORKERS = 4


def run_tests(root: Path, target: str = "tests") -> bool:
    env = {"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", str(root / target)],
                           cwd=root, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True,
                           timeout=TEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False  # a mutation that hangs the suite is detected, so it counts as killed
    return r.returncode == 0


def main() -> int:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("The crypto mutations need the cryptography package: pip install cryptography")
        return 2
    survivors = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(PKG, base, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "scripts"))
        if not run_tests(base):
            print("The unmodified test suite fails; fix that first.")
            return 2
        print("baseline: tests pass")
        def one(item):
            i, (name, edits) = item
            work = Path(tmp) / f"m{i}"
            shutil.copytree(base, work)
            try:
                for rel, old, new in edits:
                    p = work / rel
                    text = p.read_text()
                    if text.count(old) != 1:
                        return name, None
                    p.write_text(text.replace(old, new))
                # a mutation of net.py or canary.py can only be caught by their own tests, which are also much faster to run
                return name, not run_tests(work, "tests/test_net.py" if name.startswith("net:") else "tests/test_canary.py" if name.startswith("canary:") else "tests")
            finally:
                shutil.rmtree(work, ignore_errors=True)

        only = sys.argv[1] if len(sys.argv) > 1 else ""  # optional name prefix, for example: net:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for name, killed in pool.map(one, [(i, m) for i, m in enumerate(MUTATIONS) if m[0].startswith(only)]):
                if killed is None:
                    print(f"ERROR    {name}: anchor not found exactly once")
                    survivors.append(name + " (anchor error)")
                    continue
                print(("KILLED   " if killed else "SURVIVED ") + name, flush=True)
                if not killed:
                    survivors.append(name)
    ran = len([m for m in MUTATIONS if m[0].startswith(only)])
    print(f"\n{ran - len(survivors)} of {ran} mutations killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
