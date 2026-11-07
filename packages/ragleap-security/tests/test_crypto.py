import os

import pytest

pytest.importorskip("cryptography")

from ragleap_security import CryptoError, Key, KeyRing, LimitError, Scope  # noqa: E402
from ragleap_security import crypto  # noqa: E402
from ragleap_security.crypto import (decrypt, decrypt_with_passphrase, encrypt, encrypt_with_passphrase,  # noqa: E402
                                     key_id, reencrypt)

PW = "pw-" + os.urandom(6).hex()


def test_round_trip_and_fresh_nonce():
    k = Key.generate("k1")
    a, b = encrypt(k, b"hello"), encrypt(k, b"hello")
    assert decrypt(k, a) == decrypt(k, b) == b"hello"
    assert a[10:22] != b[10:22]  # the 12-byte nonce differs for every message


def test_empty_and_bytes_like_inputs():
    k = Key.generate("k1")
    assert decrypt(k, encrypt(k, b"")) == b""
    assert decrypt(k, encrypt(k, bytearray(b"ab"))) == b"ab"
    assert decrypt(k, bytearray(encrypt(k, b"ab"))) == b"ab"
    assert decrypt(k, memoryview(encrypt(k, b"ab"))) == b"ab"


def test_envelope_layout():
    k = Key.generate("key-1")
    e = encrypt(k, b"abc")
    assert e[:4] == b"RLSE" and e[4] == 1 and e[5] == 1 and e[6] == 0 and e[7] == 5 and e[8:13] == b"key-1"
    assert len(e) == 4 + 3 + 1 + 5 + 12 + 3 + 16
    assert key_id(e) == "key-1"


def test_plaintext_does_not_appear():
    k = Key.generate("k")
    secret = os.urandom(24)
    assert secret not in encrypt(k, secret)


def test_str_data_refused():
    with pytest.raises(TypeError):
        encrypt(Key.generate("k"), "text")
    with pytest.raises(TypeError):
        encrypt("not a key", b"x")
    with pytest.raises(TypeError):
        decrypt("not a key", encrypt(Key.generate("k"), b"x"))


def test_wrong_key_same_id():
    e = encrypt(Key.generate("k"), b"x")
    with pytest.raises(CryptoError) as err:
        decrypt(Key.generate("k"), e)
    assert str(err.value) == "cannot decrypt"


def test_wrong_key_id_and_unknown_id_give_same_message():
    k = Key.generate("k")
    e = encrypt(k, b"x")
    with pytest.raises(CryptoError) as e1:
        decrypt(Key.generate("other"), e)
    with pytest.raises(CryptoError) as e2:
        decrypt(KeyRing([Key.generate("zz")]), e)
    with pytest.raises(CryptoError) as e3:
        decrypt(Key(k._material(), "k"), e[:-1] + bytes([e[-1] ^ 1]))
    assert str(e1.value) == str(e2.value) == str(e3.value) == "cannot decrypt"


def test_context_is_bound():
    k = Key.generate("k")
    e = encrypt(k, b"x", b"tenant-a")
    assert decrypt(k, e, b"tenant-a") == b"x"
    for bad in (b"tenant-b", b"", b"tenant-a "):
        with pytest.raises(CryptoError):
            decrypt(k, e, bad)
    with pytest.raises(CryptoError):
        decrypt(k, encrypt(k, b"x"), b"tenant-a")


def test_context_accepts_str_and_scope_the_same_way():
    k = Key.generate("k")
    sc = Scope("acme", subject="u1")
    e = encrypt(k, b"x", sc)
    assert decrypt(k, e, sc.key) == b"x" and decrypt(k, e, sc.key.encode()) == b"x"
    with pytest.raises(CryptoError):
        decrypt(k, e, Scope("acme", subject="u2"))
    with pytest.raises(TypeError):
        encrypt(k, b"x", 5)


def test_context_framing_is_not_ambiguous():
    k = Key.generate("k")
    e = encrypt(k, b"x", b"ab")
    with pytest.raises(CryptoError):
        decrypt(k, e, b"a")


def test_context_size_limit_boundary():
    k = Key.generate("k")
    assert decrypt(k, encrypt(k, b"x", b"c" * 4096), b"c" * 4096) == b"x"
    with pytest.raises(LimitError):
        encrypt(k, b"x", b"c" * 4097)


def test_data_size_limit(monkeypatch):
    k = Key.generate("k")
    monkeypatch.setattr(crypto, "MAX_PLAINTEXT_BYTES", 10)
    assert decrypt(k, encrypt(k, b"1234567890")) == b"1234567890"
    with pytest.raises(LimitError):
        encrypt(k, b"12345678901")
    big = encrypt(k, b"x")
    monkeypatch.setattr(crypto, "MAX_PLAINTEXT_BYTES", 0)
    with pytest.raises(LimitError):
        decrypt(k, big + b"\x00" * 2000)


def test_every_single_byte_change_fails():
    k = Key.generate("k1")
    e = encrypt(k, b"some data here", b"ctx")
    for i in range(len(e)):
        for flip in (0x01, 0x80, 0xFF):
            bad = e[:i] + bytes([e[i] ^ flip]) + e[i + 1:]
            with pytest.raises(CryptoError):
                decrypt(k, bad, b"ctx")


def test_every_truncation_and_extension_fails():
    k = Key.generate("k1")
    e = encrypt(k, b"some data here", b"ctx")
    for n in range(len(e)):
        with pytest.raises(CryptoError):
            decrypt(k, e[:n], b"ctx")
    with pytest.raises(CryptoError):
        decrypt(k, e + b"\x00", b"ctx")


@pytest.mark.parametrize("pos,value", [(4, 2), (4, 0), (5, 2), (5, 0)])
def test_unknown_version_or_algorithm(pos, value):
    k = Key.generate("k")
    e = bytearray(encrypt(k, b"x"))
    e[pos] = value
    with pytest.raises(CryptoError) as err:
        decrypt(k, bytes(e))
    assert "unsupported" in str(err.value)


def test_not_an_envelope():
    for junk in (b"", b"RLSE", b"hello world, not an envelope", os.urandom(64)):
        with pytest.raises(CryptoError):
            decrypt(Key.generate("k"), junk)
        with pytest.raises(CryptoError):
            key_id(junk)


def test_key_id_must_be_ascii_and_in_range():
    k = Key.generate("k")
    e = bytearray(encrypt(k, b"x"))
    e[8] = 0xFF
    with pytest.raises(CryptoError):
        decrypt(k, bytes(e))
    e2 = bytearray(encrypt(k, b"x"))
    e2[7] = 0
    with pytest.raises(CryptoError):
        decrypt(k, bytes(e2))
    e3 = bytearray(encrypt(k, b"x"))
    e3[7] = 65
    with pytest.raises(CryptoError):
        decrypt(k, bytes(e3))


def test_keyring_encrypts_with_active_and_decrypts_by_id():
    old, new = Key.generate("old"), Key.generate("new")
    e_old = encrypt(old, b"data", b"c")
    ring = KeyRing([new, old])
    assert key_id(encrypt(ring, b"x")) == "new"
    assert decrypt(ring, e_old, b"c") == b"data"
    assert key_id(encrypt(KeyRing([old, new], active="new"), b"x")) == "new"


def test_reencrypt_rotates_to_active_key_and_keeps_context():
    old, new = Key.generate("old"), Key.generate("new")
    e_old = encrypt(old, b"data", b"c")
    ring = KeyRing([new, old])
    e_new = reencrypt(ring, e_old, b"c")
    assert key_id(e_new) == "new" and decrypt(new, e_new, b"c") == b"data"
    with pytest.raises(CryptoError):
        decrypt(new, e_new, b"other")
    with pytest.raises(CryptoError):
        reencrypt(ring, e_old, b"wrong context")
    with pytest.raises(TypeError):
        reencrypt(old, e_old)


def test_passphrase_round_trip_and_wrong_passphrase():
    e = encrypt_with_passphrase(PW, b"data", b"ctx")
    assert decrypt_with_passphrase(PW, e, b"ctx") == b"data"
    assert key_id(e) is None and e[6] == 1
    with pytest.raises(CryptoError) as err:
        decrypt_with_passphrase(PW + "x", e, b"ctx")
    assert str(err.value) == "cannot decrypt"
    with pytest.raises(CryptoError):
        decrypt_with_passphrase(PW, e, b"other")
    assert encrypt_with_passphrase(PW, b"d") != encrypt_with_passphrase(PW, b"d")


def test_passphrase_settings_are_stored_and_authenticated():
    e = encrypt_with_passphrase(PW, b"data", log2_n=14, r=4, p=2)
    assert (e[7], e[8], e[9]) == (14, 4, 2) and decrypt_with_passphrase(PW, e) == b"data"
    for i in range(len(e)):
        bad = e[:i] + bytes([e[i] ^ 1]) + e[i + 1:]
        with pytest.raises(CryptoError):
            decrypt_with_passphrase(PW, bad)


def test_hostile_scrypt_settings_in_an_envelope_are_refused_before_work():
    e = bytearray(encrypt_with_passphrase(PW, b"data"))
    e[7], e[8] = 30, 255
    with pytest.raises(CryptoError):
        decrypt_with_passphrase(PW, bytes(e))


def test_passphrase_and_key_envelopes_are_not_interchangeable():
    k = Key.generate("k")
    with pytest.raises(CryptoError) as err:
        decrypt(k, encrypt_with_passphrase(PW, b"x"))
    assert "passphrase" in str(err.value)
    with pytest.raises(CryptoError) as err:
        decrypt_with_passphrase(PW, encrypt(k, b"x"))
    assert "key id" in str(err.value)


def test_passphrase_key_matches_key_from_passphrase():
    e = encrypt_with_passphrase(PW, b"x")
    salt = e[10:26]
    k = Key.from_passphrase(PW, salt, "p")
    # same derived key, so the raw cipher agrees with the envelope body
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce, body = e[26:38], e[38:]
    assert AESGCM(k._material()).decrypt(nonce, body, e[:38] + (0).to_bytes(4, "big")) == b"x"


def test_errors_never_contain_secrets():
    k = Key.generate("k")
    secret = os.urandom(8).hex().encode()
    e = encrypt(k, secret, b"c")
    with pytest.raises(CryptoError) as err:
        decrypt(Key.generate("k"), e, b"c")
    assert secret.decode() not in str(err.value) and k.reveal_b64() not in str(err.value)


def test_header_is_authenticated_even_when_two_ids_share_key_bytes():
    raw = os.urandom(32)
    ring = KeyRing([Key(raw, "a"), Key(raw, "b")])
    e = encrypt(ring, b"x")
    assert key_id(e) == "a"
    swapped = e[:8] + b"b" + e[9:]
    assert key_id(swapped) == "b"
    with pytest.raises(CryptoError):
        decrypt(ring, swapped)


def test_a_key_with_another_id_is_refused_even_with_the_same_bytes():
    raw = os.urandom(32)
    e = encrypt(Key(raw, "a"), b"x")
    assert decrypt(Key(raw, "a"), e) == b"x"
    with pytest.raises(CryptoError):
        decrypt(Key(raw, "b"), e)


def test_passphrase_envelopes_use_a_fresh_salt_and_nonce():
    a, b = encrypt_with_passphrase(PW, b"d"), encrypt_with_passphrase(PW, b"d")
    assert a[10:26] != b[10:26] and a[26:38] != b[26:38]


def test_passphrase_data_and_context_limits(monkeypatch):
    monkeypatch.setattr(crypto, "MAX_PLAINTEXT_BYTES", 4)
    assert decrypt_with_passphrase(PW, encrypt_with_passphrase(PW, b"1234")) == b"1234"
    with pytest.raises(LimitError):
        encrypt_with_passphrase(PW, b"12345")
    monkeypatch.undo()
    with pytest.raises(LimitError):
        encrypt_with_passphrase(PW, b"x", b"c" * 4097)


def test_malformed_envelopes_say_so():
    k = Key.generate("k")
    e = encrypt(k, b"x")
    for i in (0, 1, 2, 3):  # magic
        bad = bytearray(e)
        bad[i] ^= 1
        with pytest.raises(CryptoError) as err:
            decrypt(k, bytes(bad))
        assert "not a ragleap-security envelope" in str(err.value)
    for pos, value in ((7, 0), (7, 65), (8, 0xFF)):
        bad = bytearray(e)
        bad[pos] = value
        with pytest.raises(CryptoError) as err:
            decrypt(k, bytes(bad))
        assert "not a ragleap-security envelope" in str(err.value)
    bad = bytearray(e)
    bad[6] = 2  # unknown kdf
    with pytest.raises(CryptoError) as err:
        decrypt(k, bytes(bad))
    assert "not a ragleap-security envelope" in str(err.value)
    with pytest.raises(CryptoError) as err:
        decrypt(k, e[:30])
    assert "not a ragleap-security envelope" in str(err.value)
