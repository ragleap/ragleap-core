"""Keys: a Key that never prints its value, and a KeyRing for rotation. Standard library only.

A Key is 32 random bytes plus a key id. The id is written into every envelope, so a ring of keys
can decrypt old data while new data uses the active key."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from typing import Iterable, Optional, Union

from .secrets import SecretStr, from_env as _secret_from_env

KEY_BYTES = 32
MIN_PASSPHRASE_CHARS = 8
MIN_SALT_BYTES = 16
SCRYPT_DEFAULT = (15, 8, 1)  # log2(n), r, p: about 32 MiB of memory
_SCRYPT_MAX_MEMORY = 256 * 1024 * 1024
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}\Z")


class CryptoError(Exception):
    """A key, passphrase or envelope is invalid, or data cannot be decrypted. Messages never contain
    keys, passphrases or data."""


def _check_id(key_id: object) -> str:
    if not isinstance(key_id, str) or not _KEY_ID.match(key_id):
        raise CryptoError("invalid key id")
    return key_id


def check_scrypt_params(log2_n: int, r: int, p: int) -> None:
    """Refuse scrypt settings that are too weak or that would use too much memory (an envelope from
    outside could otherwise ask for gigabytes)."""
    for name, v in (("log2_n", log2_n), ("r", r), ("p", p)):
        if isinstance(v, bool) or not isinstance(v, int):
            raise CryptoError(f"{name} must be an int")
    if not 14 <= log2_n <= 18 or not 1 <= r <= 16 or not 1 <= p <= 4:
        raise CryptoError("scrypt settings are outside the allowed range")
    if 128 * (1 << log2_n) * r > _SCRYPT_MAX_MEMORY:
        raise CryptoError("scrypt settings would use too much memory")


def derive_scrypt(passphrase: Union[str, SecretStr], salt: bytes, log2_n: int = 15, r: int = 8, p: int = 1) -> bytes:
    """32 key bytes from a passphrase with hashlib.scrypt. The passphrase needs at least 8 characters
    (a floor, not a strength check) and the salt at least 16 random bytes."""
    pw = passphrase.reveal() if isinstance(passphrase, SecretStr) else passphrase
    if not isinstance(pw, str) or len(pw) < MIN_PASSPHRASE_CHARS:
        raise CryptoError("the passphrase must be a string of at least 8 characters")
    if not isinstance(salt, (bytes, bytearray)) or len(salt) < MIN_SALT_BYTES:
        raise CryptoError("the salt must be at least 16 bytes")
    check_scrypt_params(log2_n, r, p)
    n = 1 << log2_n
    return hashlib.scrypt(pw.encode("utf-8"), salt=bytes(salt), n=n, r=r, p=p,
                          maxmem=2 * 128 * n * r + 4 * 1024 * 1024, dklen=KEY_BYTES)


def _decode_b64(text: str) -> bytes:
    try:
        raw = base64.b64decode(text.strip().replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError):
        raise CryptoError("the key is not valid base64") from None
    return raw


class Key:
    """A 32-byte key with an id. str(), repr() and format() never show the bytes; it cannot be
    pickled or hashed. reveal_b64() is the only way to read the value."""

    __slots__ = ("key_id", "_k")

    def __init__(self, raw: bytes, key_id: str):
        self.key_id = _check_id(key_id)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != KEY_BYTES:
            raise CryptoError("a key must be exactly 32 bytes")
        self._k = bytes(raw)

    @classmethod
    def generate(cls, key_id: str) -> "Key":
        return cls(os.urandom(KEY_BYTES), key_id)

    @classmethod
    def from_env(cls, name: str, key_id: str) -> "Key":
        """Read a base64 key (standard or URL-safe) from the environment variable `name`."""
        secret = _secret_from_env(name, required=True)
        assert secret is not None
        return cls(_decode_b64(secret.reveal()), key_id)

    @classmethod
    def from_passphrase(cls, passphrase: Union[str, SecretStr], salt: bytes, key_id: str, *,
                        log2_n: int = 15, r: int = 8, p: int = 1) -> "Key":
        """Derive a key with scrypt. Keep the salt (it is not secret): the same passphrase and salt
        give the same key."""
        return cls(derive_scrypt(passphrase, salt, log2_n, r, p), key_id)

    def reveal_b64(self) -> str:
        return base64.b64encode(self._k).decode("ascii")

    def derive(self, purpose: bytes) -> bytes:
        """A separate 32-byte key for one purpose, so one Key is never used raw for two jobs."""
        if not isinstance(purpose, (bytes, bytearray)) or not purpose:
            raise CryptoError("purpose must be non-empty bytes")
        return hmac.new(self._k, b"ragleap-security/derive/v1/" + bytes(purpose), hashlib.sha256).digest()

    def _material(self) -> bytes:
        return self._k

    def __repr__(self) -> str:
        return f"Key(key_id={self.key_id!r})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:
        if type(other) is not Key:
            return NotImplemented
        return self.key_id == other.key_id and hmac.compare_digest(self._k, other._k)

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self):
        raise TypeError("Key cannot be pickled")

    def __copy__(self) -> "Key":
        return Key(self._k, self.key_id)

    def __deepcopy__(self, memo) -> "Key":
        return Key(self._k, self.key_id)


class KeyRing:
    """Several keys, one of them active. New data is encrypted with the active key; old data is
    decrypted with the key its envelope names. Rotating means building a new ring whose active key
    is the new key and keeping the old keys in it until all old data is re-encrypted."""

    def __init__(self, keys: Iterable[Key], active: Optional[str] = None):
        found = {}
        for k in keys:
            if type(k) is not Key:
                raise CryptoError("a KeyRing holds Key objects")
            if k.key_id in found:
                raise CryptoError("two keys have the same id")
            found[k.key_id] = k
        if not found:
            raise CryptoError("a KeyRing needs at least one key")
        if active is None:
            active = next(iter(found))
        if active not in found:
            raise CryptoError("the active key id is not in the ring")
        self._keys = found
        self.active_id = active

    @property
    def active(self) -> Key:
        return self._keys[self.active_id]

    @property
    def key_ids(self) -> tuple:
        return tuple(self._keys)

    def get(self, key_id: str) -> Optional[Key]:
        return self._keys.get(key_id)

    def __len__(self) -> int:
        return len(self._keys)

    def __repr__(self) -> str:
        return f"KeyRing(key_ids={self.key_ids!r}, active={self.active_id!r})"
