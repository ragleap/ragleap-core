import base64
import copy
import hashlib
import os
import pickle

import pytest

from ragleap_security import CryptoError, Key, KeyRing, SecretError
from ragleap_security.keys import check_scrypt_params, derive_scrypt

PW = "pw-" + os.urandom(6).hex()
SALT = os.urandom(16)


def test_generate_makes_32_random_bytes():
    a, b = Key.generate("k1"), Key.generate("k1")
    assert len(a._material()) == 32 and a._material() != b._material()


@pytest.mark.parametrize("bad", ["", "-x", "a b", "a/b", "x" * 65, None, 5, "\u00e9"])
def test_bad_key_ids(bad):
    with pytest.raises(CryptoError):
        Key(os.urandom(32), bad)


def test_longest_valid_id():
    assert Key(os.urandom(32), "a" * 64).key_id == "a" * 64


@pytest.mark.parametrize("raw", [b"", os.urandom(31), os.urandom(33), "x" * 32, None])
def test_bad_key_length_or_type(raw):
    with pytest.raises(CryptoError):
        Key(raw, "k")


def test_value_never_printed():
    raw = os.urandom(32)
    k = Key(raw, "k1")
    for text in (str(k), repr(k), f"{k}", "%s" % k, format(k, ">40")):
        assert raw.hex() not in text and k.reveal_b64() not in text and "k1" in text


def test_cannot_pickle_or_hash():
    k = Key.generate("k")
    with pytest.raises(TypeError):
        pickle.dumps(k)
    with pytest.raises(TypeError):
        hash(k)


def test_copy_keeps_value():
    k = Key.generate("k")
    assert copy.copy(k) == k and copy.deepcopy(k) == k


def test_equality_needs_same_id_and_bytes():
    raw = os.urandom(32)
    assert Key(raw, "a") == Key(raw, "a")
    assert Key(raw, "a") != Key(raw, "b")
    assert Key(raw, "a") != Key(os.urandom(32), "a")
    assert Key(raw, "a") != raw


def test_from_env_reads_standard_and_urlsafe_base64(monkeypatch):
    raw = bytes(range(251, 251 - 32, -1))
    monkeypatch.setenv("RL_TEST_KEY", base64.b64encode(raw).decode())
    assert Key.from_env("RL_TEST_KEY", "e")._material() == raw
    monkeypatch.setenv("RL_TEST_KEY", base64.urlsafe_b64encode(raw).decode())
    assert Key.from_env("RL_TEST_KEY", "e")._material() == raw


def test_from_env_errors_never_contain_the_value(monkeypatch):
    junk = "not-base64-" + os.urandom(6).hex() + "!!"
    monkeypatch.setenv("RL_TEST_KEY", junk)
    with pytest.raises(CryptoError) as e:
        Key.from_env("RL_TEST_KEY", "e")
    assert junk not in str(e.value)
    monkeypatch.setenv("RL_TEST_KEY", base64.b64encode(os.urandom(16)).decode())
    with pytest.raises(CryptoError):
        Key.from_env("RL_TEST_KEY", "e")
    monkeypatch.delenv("RL_TEST_KEY")
    with pytest.raises(SecretError):
        Key.from_env("RL_TEST_KEY", "e")


def test_reveal_b64_round_trip():
    k = Key.generate("k")
    assert base64.b64decode(k.reveal_b64()) == k._material()


def test_derive_is_separate_stable_and_purpose_bound():
    k = Key.generate("k")
    assert k.derive(b"a") == k.derive(b"a") and k.derive(b"a") != k.derive(b"b")
    assert k.derive(b"a") != k._material() and len(k.derive(b"a")) == 32
    assert k.derive(b"a") != Key.generate("k").derive(b"a")
    for bad in (b"", "a", None):
        with pytest.raises(CryptoError):
            k.derive(bad)


def test_passphrase_is_deterministic_and_matches_scrypt():
    a = Key.from_passphrase(PW, SALT, "p")
    b = Key.from_passphrase(PW, SALT, "p")
    assert a == b
    assert a._material() == hashlib.scrypt(PW.encode(), salt=SALT, n=2 ** 15, r=8, p=1, maxmem=2 ** 27, dklen=32)
    assert Key.from_passphrase(PW + "x", SALT, "p") != a
    assert Key.from_passphrase(PW, os.urandom(16), "p") != a


def test_passphrase_accepts_secretstr():
    from ragleap_security import SecretStr
    assert derive_scrypt(SecretStr(PW), SALT) == derive_scrypt(PW, SALT)


@pytest.mark.parametrize("pw", ["", "short", "1234567", None, 12345678])
def test_passphrase_floor(pw):
    with pytest.raises(CryptoError):
        derive_scrypt(pw, SALT)


def test_passphrase_floor_boundary():
    assert len(derive_scrypt("12345678", SALT)) == 32


@pytest.mark.parametrize("salt", [b"", os.urandom(15), "x" * 16, None])
def test_salt_floor(salt):
    with pytest.raises(CryptoError):
        derive_scrypt(PW, salt)


def test_salt_floor_boundary():
    assert len(derive_scrypt(PW, os.urandom(16))) == 32


@pytest.mark.parametrize("params", [(13, 8, 1), (19, 8, 1), (15, 0, 1), (15, 17, 1), (15, 8, 0), (15, 8, 5),
                                    (18, 16, 1), ("15", 8, 1), (15, 8.0, 1), (True, 8, 1), (15, 8, True), (19, 1, 1)])
def test_scrypt_params_refused(params):
    with pytest.raises(CryptoError):
        check_scrypt_params(*params)


@pytest.mark.parametrize("params", [(14, 8, 1), (18, 8, 1), (15, 1, 1), (15, 16, 4), (14, 16, 4)])
def test_scrypt_params_allowed(params):
    check_scrypt_params(*params)


def test_keyring_basics_and_active():
    a, b = Key.generate("a"), Key.generate("b")
    ring = KeyRing([a, b])
    assert ring.active == a and ring.key_ids == ("a", "b") and len(ring) == 2
    assert ring.get("b") == b and ring.get("zz") is None
    assert KeyRing([a, b], active="b").active == b
    assert "a" in repr(ring) and a.reveal_b64() not in repr(ring)


def test_keyring_refusals():
    a = Key.generate("a")
    for args in (([],), ([a, Key.generate("a")],), ([a], "zz"), (["not a key"],), ([a, object()],)):
        with pytest.raises(CryptoError):
            KeyRing(*args)


def test_keyring_refuses_key_subclasses():
    class Sub(Key):
        __slots__ = ()
    with pytest.raises(CryptoError):
        KeyRing([Sub(os.urandom(32), "s")])
