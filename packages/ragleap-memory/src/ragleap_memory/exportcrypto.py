"""ragleap_memory.exportcrypto - framing for encrypted export files (format version 2).

No cryptography is implemented here. Every byte is encrypted and decrypted by ragleap_security.crypto
(AES-256-GCM); this module only decides how an export file is laid out and what each line is bound to.
The optional package is needed only when you ask for encryption: pip install ragleap-memory[crypto].

File layout (version 2):
  line 1      plain JSON header: format, version 2, count, file_id, kdf ("none" or "scrypt" and its settings)
  lines 2..   one base64 envelope per entry, each ending in a single newline
The authenticated context of entry i is: a fixed label, the SHA-256 of the exact header line, i and the caller's
own context. The header holds the count and a random file_id, so a changed count, a changed file_id or any other
changed header byte fails every line; a moved, duplicated or foreign line fails because of i and the file_id.
Dropped and extra lines fail the count check. An old file put back in place is NOT noticed (no freshness).
The header of an EMPTY export (count 0) is not authenticated: there is nothing in the file to protect.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import struct
from typing import Any, Dict, List, Optional, Tuple

from ragleap_security import LimitError, SecretStr, check_size

from .model import InvalidEntryError, MemoryStoreError

FORMAT = "ragleap-memory-export"
ENC_VERSION = 2
LABEL = b"ragleap-memory-export/2"
FILE_ID_BYTES = 16
SALT_BYTES = 16
SCRYPT_SETTINGS = (15, 8, 1)  # log2(n), r, p: the default of ragleap-security 0.2.0
PASSPHRASE_KEY_ID = "export"
MAX_CONTEXT_BYTES = 1024
MAX_PLAIN_LINE_BYTES = 65536  # the limit of a plain export line (kept equal to sqlite_store.MAX_LINE_BYTES)
MAX_ENC_LINE_BYTES = 90000    # a 64 KiB entry plus the envelope, grown by a third in base64
_LINE = re.compile(rb"[A-Za-z0-9+/]{4,}={0,2}\n")
_UNSUPPORTED = "not a ragleap-memory export file of a supported version"
_COUNT = "entry count does not match the header (truncated or edited file)"


def _load():
    """ragleap_security.crypto and its helpers, or a clear message if the optional parts are missing."""
    try:
        from ragleap_security import CryptoError, Key, KeyRing, crypto
    except ImportError:
        raise MemoryStoreError("encrypted export needs ragleap-security 0.2.0 or newer with the optional "
                               "cryptography package: pip install ragleap-memory[crypto]") from None
    return crypto, CryptoError, Key, KeyRing


class _Chain:
    """Gives back the lines already read, then the rest of the stream (only readline is used)."""

    def __init__(self, lines: List[bytes], stream: Any):
        self._lines = lines
        self._i = 0
        self._stream = stream

    def readline(self, limit: int = -1) -> bytes:
        if self._i < len(self._lines):
            line = self._lines[self._i]
            self._i += 1
            return line
        return self._stream.readline(limit)


def _context_bytes(context: object) -> bytes:
    if context is None:
        return b""
    if isinstance(context, str):
        context = context.encode("utf-8")
    elif not isinstance(context, (bytes, bytearray)):
        raise InvalidEntryError("context must be str or bytes")
    context = bytes(context)
    check_size(context, MAX_CONTEXT_BYTES, "context")
    return context


def check_args(who: str, key: object, passphrase: object, context: object) -> bool:
    """Check the encryption arguments. `who` is "encrypt_with" or "decrypt_with". True if encryption is wanted."""
    if key is not None and passphrase is not None:
        raise InvalidEntryError(f"give {who} or passphrase, not both")
    if key is None and passphrase is None:
        if context is not None:
            raise InvalidEntryError(f"context needs {who} or passphrase")
        return False
    _context_bytes(context)
    if key is not None:
        _, _, Key, KeyRing = _load()
        if type(key) is not Key and type(key) is not KeyRing:
            raise InvalidEntryError(f"{who} must be a Key or a KeyRing")
    elif not isinstance(passphrase, (str, SecretStr)):
        raise InvalidEntryError("passphrase must be a str or a SecretStr")
    return True


def _ctx(header_hash: bytes, i: int, user: bytes) -> bytes:
    return LABEL + header_hash + struct.pack(">Q", i) + user


def _b64_exact(value: object, size: int) -> bytes:
    """Decode canonical base64 of exactly `size` bytes (Python's decoder alone accepts a changed padding bit)."""
    if not isinstance(value, str):
        raise InvalidEntryError(_UNSUPPORTED)
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidEntryError(_UNSUPPORTED) from None
    if len(raw) != size or base64.b64encode(raw).decode("ascii") != value:
        raise InvalidEntryError(_UNSUPPORTED)
    return raw


class ExportPlan:
    """Everything decided before the file is created: the header line and how each line is encrypted."""

    def __init__(self, header_line: bytes, make_line: Any):
        self.header_line = header_line
        self.line = make_line


def prepare_export(count: int, encrypt_with: Any, passphrase: Any, context: object) -> ExportPlan:
    crypto, CryptoError, Key, KeyRing = _load()
    user = _context_bytes(context)
    file_id = os.urandom(FILE_ID_BYTES)
    header: Dict[str, Any] = {"count": count, "file_id": base64.b64encode(file_id).decode("ascii"),
                              "format": FORMAT, "version": ENC_VERSION}
    if passphrase is not None:
        salt = os.urandom(SALT_BYTES)
        log2_n, r, p = SCRYPT_SETTINGS
        try:
            key = Key.from_passphrase(passphrase, salt, PASSPHRASE_KEY_ID, log2_n=log2_n, r=r, p=p)
        except CryptoError as e:
            raise InvalidEntryError(str(e)) from None
        header.update(kdf="scrypt", log2_n=log2_n, r=r, p=p, salt=base64.b64encode(salt).decode("ascii"))
    else:
        key = encrypt_with
        header["kdf"] = "none"
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
    header_hash = hashlib.sha256(header_bytes).digest()

    def make_line(i: int, entry: Any) -> bytes:
        plain = json.dumps(entry, sort_keys=True).encode("utf-8")
        envelope = crypto.encrypt(key, plain, _ctx(header_hash, i, user))
        return base64.b64encode(envelope) + b"\n"

    return ExportPlan(header_bytes + b"\n", make_line)


def _header_of(raw: bytes) -> Optional[Dict[str, Any]]:
    """The parsed header if this line is a well-formed version 2 header, else None."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if isinstance(obj, dict) and obj.get("format") == FORMAT and obj.get("version") == ENC_VERSION:
        return obj
    return None


def open_import(stream: Any, decrypt_with: Any, passphrase: Any, context: object,
                max_entries: int, max_bytes: int) -> Tuple[Any, bool]:
    """Return (a plain version 1 stream for the normal import code, whether the file was encrypted).

    The caller's own checks (trust cap, scope, quotas, one transaction, and the entry, size and line limits of
    the plain reader, which also reports a missing line) then run on the decrypted entries exactly as for a plain
    file: decrypting does not make a file trusted. This function adds only the limits on the encrypted file."""
    want = check_args("decrypt_with", decrypt_with, passphrase, context)
    prefix: List[bytes] = []
    total = 0
    header_raw = None
    while True:
        raw = stream.readline(MAX_PLAIN_LINE_BYTES + 1)
        if not raw:
            break
        prefix.append(raw)
        total += len(raw)
        if total > max_bytes:
            break  # the plain reader reports the limit
        if raw.strip():
            header_raw = raw
            break
    header = _header_of(header_raw) if header_raw is not None else None
    if header is None:
        if want:
            raise InvalidEntryError("not an encrypted ragleap-memory export file (a plain file is refused "
                                    "when a key or passphrase is given)")
        return _Chain(prefix, stream), False
    if not want:
        raise InvalidEntryError("this export file is encrypted: give decrypt_with or passphrase")
    return _decrypt_file(stream, prefix, header_raw, header, decrypt_with, passphrase, context,
                         max_entries, max_bytes), True


def _decrypt_file(stream: Any, prefix: List[bytes], header_raw: bytes, header: Dict[str, Any], decrypt_with: Any,
                  passphrase: Any, context: object, max_entries: int, max_bytes: int) -> io.BytesIO:
    crypto, CryptoError, Key, KeyRing = _load()
    user = _context_bytes(context)
    count = header.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or not header_raw.endswith(b"\n"):
        raise InvalidEntryError(_UNSUPPORTED)
    if count > max_entries:
        raise LimitError(f"file has more than {max_entries} entries")
    _b64_exact(header.get("file_id"), FILE_ID_BYTES)
    kdf = header.get("kdf")
    if kdf == "scrypt":
        if passphrase is None:
            raise InvalidEntryError("this file was encrypted with a passphrase: give passphrase")
        salt = _b64_exact(header.get("salt"), SALT_BYTES)
        try:  # the library refuses settings that are too weak or would use too much memory, before any work
            keys = Key.from_passphrase(passphrase, salt, PASSPHRASE_KEY_ID, log2_n=header.get("log2_n"),
                                       r=header.get("r"), p=header.get("p"))
        except CryptoError as e:
            raise InvalidEntryError(str(e)) from None
    elif kdf == "none":
        if decrypt_with is None:
            raise InvalidEntryError("this file was encrypted with a key: give decrypt_with")
        keys = decrypt_with
    else:
        raise InvalidEntryError(_UNSUPPORTED)
    header_hash = hashlib.sha256(header_raw[:-1]).digest()
    n = len(prefix)
    raw_total = sum(len(p) for p in prefix)
    pieces: List[bytes] = [b"\n" * (n - 1),
                           json.dumps({"count": count, "format": FORMAT, "version": 1}, sort_keys=True).encode() + b"\n"]
    i = 0
    while True:
        raw = stream.readline(MAX_ENC_LINE_BYTES + 1)
        if not raw:
            break
        n += 1
        raw_total += len(raw)
        if raw_total > 2 * max_bytes:
            raise LimitError(f"file is larger than {2 * max_bytes} bytes")
        if len(raw) > MAX_ENC_LINE_BYTES:
            raise LimitError(f"line {n} is longer than {MAX_ENC_LINE_BYTES} bytes")
        if raw == b"\n":
            pieces.append(b"\n")
            continue
        i += 1
        if i > count:
            raise InvalidEntryError(_COUNT)
        if not _LINE.fullmatch(raw):
            raise InvalidEntryError(f"line {n}: cannot decrypt")
        try:
            envelope = base64.b64decode(raw[:-1], validate=True)
        except (binascii.Error, ValueError):
            raise InvalidEntryError(f"line {n}: cannot decrypt") from None
        if base64.b64encode(envelope) != raw[:-1]:
            raise InvalidEntryError(f"line {n}: cannot decrypt")
        try:
            plain = crypto.decrypt(keys, envelope, _ctx(header_hash, i, user))
        except CryptoError:
            raise InvalidEntryError(f"line {n}: cannot decrypt") from None
        if b"\n" in plain:  # would add lines the header did not count
            raise InvalidEntryError(f"line {n}: not valid JSON")
        pieces.append(plain + b"\n")
    return io.BytesIO(b"".join(pieces))
