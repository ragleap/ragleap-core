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
]


TEST_TIMEOUT_SECONDS = 120
WORKERS = 4


def run_tests(root: Path) -> bool:
    env = {"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", str(root / "tests")],
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
                return name, not run_tests(work)
            finally:
                shutil.rmtree(work, ignore_errors=True)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for name, killed in pool.map(one, enumerate(MUTATIONS)):
                if killed is None:
                    print(f"ERROR    {name}: anchor not found exactly once")
                    survivors.append(name + " (anchor error)")
                    continue
                print(("KILLED   " if killed else "SURVIVED ") + name, flush=True)
                if not killed:
                    survivors.append(name)
    print(f"\n{len(MUTATIONS) - len(survivors)} of {len(MUTATIONS)} mutations killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
