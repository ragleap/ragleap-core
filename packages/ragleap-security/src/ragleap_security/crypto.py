"""Authenticated encryption (AES-256-GCM) with a versioned envelope, key ids and rotation.

Needs the optional `cryptography` package: pip install ragleap-security[crypto]. No cryptography is
implemented here; this module only frames data, binds it to a context, and calls the library.

Envelope (bytes): magic "RLSE", version, algorithm, kdf, then either [key id length, key id] (kdf 0)
or [log2 n, r, p, 16-byte salt] (kdf 1, passphrase), then a random 12-byte nonce, then ciphertext and
tag. Everything before the ciphertext, plus the caller's context, is authenticated."""
from __future__ import annotations

import os
import struct
from typing import Optional, Union

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as exc:  # pragma: no cover - exercised through a subprocess test
    raise ImportError("ragleap_security.crypto needs the 'cryptography' package: "
                      "pip install ragleap-security[crypto]") from exc

from .keys import CryptoError, Key, KeyRing, derive_scrypt, SCRYPT_DEFAULT
from .limits import check_size
from .scope import Scope
from .secrets import SecretStr

MAGIC = b"RLSE"
VERSION = 1
ALG_AES256GCM = 1
KDF_NONE = 0
KDF_SCRYPT = 1
NONCE_BYTES = 12
TAG_BYTES = 16
SALT_BYTES = 16
MAX_PLAINTEXT_BYTES = 64 * 1024 * 1024
MAX_CONTEXT_BYTES = 4096
_FORMAT = "not a ragleap-security envelope, or an unsupported version"
_GENERIC = "cannot decrypt"


def _context(context: object) -> bytes:
    if type(context) is Scope:
        context = context.key
    if isinstance(context, str):
        context = context.encode("utf-8")
    elif not isinstance(context, (bytes, bytearray, memoryview)):
        raise TypeError("context must be bytes, str or a Scope")
    context = bytes(context)
    check_size(context, MAX_CONTEXT_BYTES, "context")
    return context


def _bytes(value: object, what: str, limit: int) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{what} must be bytes")
    check_size(value, limit, what)
    return bytes(value)


def _aad(header: bytes, context: bytes) -> bytes:
    return header + struct.pack(">I", len(context)) + context


def encrypt(key: Union[Key, KeyRing], data: bytes, context: Union[bytes, str, Scope] = b"") -> bytes:
    """Encrypt `data` (at most 64 MiB). With a KeyRing the active key is used. `context` is also
    authenticated: decrypt must be given the same context."""
    k = key.active if type(key) is KeyRing else key
    if type(k) is not Key:
        raise TypeError("key must be a Key or a KeyRing")
    plain = _bytes(data, "data", MAX_PLAINTEXT_BYTES)
    ctx = _context(context)
    kid = k.key_id.encode("ascii")
    nonce = os.urandom(NONCE_BYTES)
    header = MAGIC + bytes([VERSION, ALG_AES256GCM, KDF_NONE, len(kid)]) + kid + nonce
    return header + AESGCM(k._material()).encrypt(nonce, plain, _aad(header, ctx))


def encrypt_with_passphrase(passphrase: Union[str, SecretStr], data: bytes, context: Union[bytes, str, Scope] = b"", *,
                            log2_n: int = SCRYPT_DEFAULT[0], r: int = SCRYPT_DEFAULT[1], p: int = SCRYPT_DEFAULT[2]) -> bytes:
    """Encrypt with a key derived from a passphrase (scrypt). The salt and settings are stored in the
    envelope, so only the passphrase is needed to decrypt."""
    plain = _bytes(data, "data", MAX_PLAINTEXT_BYTES)
    ctx = _context(context)
    salt = os.urandom(SALT_BYTES)
    material = derive_scrypt(passphrase, salt, log2_n, r, p)
    nonce = os.urandom(NONCE_BYTES)
    header = MAGIC + bytes([VERSION, ALG_AES256GCM, KDF_SCRYPT, log2_n, r, p]) + salt + nonce
    return header + AESGCM(material).encrypt(nonce, plain, _aad(header, ctx))


def _parse(envelope: object):
    raw = _bytes(envelope, "envelope", MAX_PLAINTEXT_BYTES + 1024)
    if len(raw) < 8 or raw[:4] != MAGIC or raw[4] != VERSION or raw[5] != ALG_AES256GCM:
        raise CryptoError(_FORMAT)
    kdf, pos = raw[6], 7
    if kdf == KDF_NONE:
        n = raw[pos]
        info = raw[pos + 1:pos + 1 + n]
        if not 1 <= n <= 64 or len(info) != n:
            raise CryptoError(_FORMAT)
        try:
            info = info.decode("ascii")
        except UnicodeDecodeError:
            raise CryptoError(_FORMAT) from None
        pos += 1 + n
    elif kdf == KDF_SCRYPT:
        if len(raw) < pos + 3 + SALT_BYTES:
            raise CryptoError(_FORMAT)
        info = (raw[pos], raw[pos + 1], raw[pos + 2], raw[pos + 3:pos + 3 + SALT_BYTES])
        pos += 3 + SALT_BYTES
    else:
        raise CryptoError(_FORMAT)
    nonce = raw[pos:pos + NONCE_BYTES]
    pos += NONCE_BYTES
    if len(nonce) != NONCE_BYTES or len(raw) < pos + TAG_BYTES:
        raise CryptoError(_FORMAT)
    return kdf, info, nonce, raw[:pos], raw[pos:]


def key_id(envelope: bytes) -> Optional[str]:
    """The key id an envelope names, or None for a passphrase envelope."""
    kdf, info, _, _, _ = _parse(envelope)
    return info if kdf == KDF_NONE else None


def _open(material: bytes, nonce: bytes, header: bytes, body: bytes, context: object) -> bytes:
    try:
        return AESGCM(material).decrypt(nonce, body, _aad(header, _context(context)))
    except InvalidTag:
        raise CryptoError(_GENERIC) from None


def decrypt(keys: Union[Key, KeyRing], envelope: bytes, context: Union[bytes, str, Scope] = b"") -> bytes:
    """Decrypt an envelope made by encrypt(). A wrong key, a wrong context and damaged data all give
    the same CryptoError."""
    kdf, info, nonce, header, body = _parse(envelope)
    if kdf != KDF_NONE:
        raise CryptoError("this envelope needs a passphrase: use decrypt_with_passphrase")
    if type(keys) is KeyRing:
        k = keys.get(info)
    elif type(keys) is Key:
        k = keys if keys.key_id == info else None
    else:
        raise TypeError("keys must be a Key or a KeyRing")
    if k is None:
        raise CryptoError(_GENERIC)
    return _open(k._material(), nonce, header, body, context)


def decrypt_with_passphrase(passphrase: Union[str, SecretStr], envelope: bytes,
                            context: Union[bytes, str, Scope] = b"") -> bytes:
    kdf, info, nonce, header, body = _parse(envelope)
    if kdf != KDF_SCRYPT:
        raise CryptoError("this envelope names a key id: use decrypt")
    log2_n, r, p, salt = info  # derive_scrypt refuses settings outside the allowed range
    return _open(derive_scrypt(passphrase, salt, log2_n, r, p), nonce, header, body, context)


def reencrypt(ring: KeyRing, envelope: bytes, context: Union[bytes, str, Scope] = b"") -> bytes:
    """Decrypt with any key in the ring and encrypt again with the active key (key rotation)."""
    if type(ring) is not KeyRing:
        raise TypeError("ring must be a KeyRing")
    return encrypt(ring, decrypt(ring, envelope, context), context)
