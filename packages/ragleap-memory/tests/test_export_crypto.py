"""Encrypted export and import (export format version 2). Needs the optional cryptography package."""
import base64
import hashlib
import io
import json
import os
import stat
import struct

import pytest

pytest.importorskip("cryptography")

from ragleap_memory import (InvalidEntryError, MemoryStoreError, QuotaExceededError, ShardedStore, SQLiteStore,  # noqa: E402
                            exportcrypto, sqlite_store)
from ragleap_security import AuditLog, Key, KeyRing, LimitError, Scope, SecretStr, crypto, verify  # noqa: E402

A = Scope("acme")
B = Scope("globex")
SECRET = "very private sentence"
KEY = Key.generate("k1")
PASS = "pw-" + os.urandom(8).hex()
REFUSED = (InvalidEntryError, LimitError)


def filled(store, scope=A, n=3):
    store.add(scope, SECRET, kind="fact", trust="owner")
    for i in range(n - 1):
        store.add(scope, f"note number {i}", kind="fact", trust="model")
    return store


def texts(store, scope=A):
    return sorted(e["text"] for e in store.export(scope))


def raw_lines(path):
    return open(path, "rb").read().split(b"\n")[:-1]


def write_lines(path, lines):
    with open(path, "wb") as f:
        f.write(b"\n".join(lines) + b"\n")
    return path


def exported(store, path, **kw):
    kw.setdefault("encrypt_with", KEY)
    store.export_file(A, path, **kw)
    return path


def forge(path, entries, key=KEY, passphrase=None, context=None, count=None):
    """A valid encrypted file with chosen entries (what a holder of the key could make)."""
    plan = exportcrypto.prepare_export(len(entries) if count is None else count, None if passphrase else key,
                                       passphrase, context)
    with open(path, "wb") as f:
        f.write(plan.header_line)
        for i, e in enumerate(entries, 1):
            f.write(plan.line(i, e))
    return path


def entry(**kw):
    e = {"scope": "tenant=acme", "kind": "fact", "text": "hello world", "trust": "owner"}
    e.update(kw)
    return e


# ---------------------------------------------------------------- round trips

def test_round_trip_with_a_key(store, tmp_path):
    filled(store)
    p = tmp_path / "e.enc"
    assert store.export_file(A, p, encrypt_with=KEY) == 3
    dst = SQLiteStore()
    assert dst.import_file(A, p, decrypt_with=KEY).imported == 3
    assert texts(dst) == texts(store)


def test_round_trip_with_a_passphrase_and_a_secretstr(store, tmp_path):
    filled(store)
    p = exported(store, tmp_path / "e.enc", encrypt_with=None, passphrase=PASS)
    assert SQLiteStore().import_file(A, p, passphrase=PASS).imported == 3
    p2 = exported(store, tmp_path / "f.enc", encrypt_with=None, passphrase=SecretStr(PASS))
    assert SQLiteStore().import_file(A, p2, passphrase=SecretStr(PASS)).imported == 3


def test_a_key_ring_uses_the_active_key_and_old_keys_still_open_old_files(store, tmp_path):
    filled(store)
    old, new = Key.generate("old"), Key.generate("new")
    p = exported(store, tmp_path / "e.enc", encrypt_with=KeyRing([old], active="old"))
    assert SQLiteStore().import_file(A, p, decrypt_with=KeyRing([new, old], active="new")).imported == 3
    with pytest.raises(InvalidEntryError, match="line 2: cannot decrypt"):
        SQLiteStore().import_file(A, p, decrypt_with=KeyRing([new], active="new"))
    q = exported(store, tmp_path / "f.enc", encrypt_with=KeyRing([old, new], active="new"))
    assert {crypto.key_id(base64.b64decode(line)) for line in raw_lines(q)[1:]} == {"new"}


def test_context_must_match_exactly(store, tmp_path):
    filled(store)
    p = exported(store, tmp_path / "e.enc", context="backup-1")
    assert SQLiteStore().import_file(A, p, decrypt_with=KEY, context="backup-1").imported == 3
    assert SQLiteStore().import_file(A, p, decrypt_with=KEY, context=b"backup-1").imported == 3
    for ctx in (None, "backup-2", "backup-1 "):
        kw = {} if ctx is None else {"context": ctx}
        with pytest.raises(InvalidEntryError, match="line 2: cannot decrypt"):
            SQLiteStore().import_file(A, p, decrypt_with=KEY, **kw)
    q = exported(store, tmp_path / "f.enc")
    with pytest.raises(InvalidEntryError, match="line 2: cannot decrypt"):
        SQLiteStore().import_file(A, q, decrypt_with=KEY, context="backup-1")


def test_an_empty_export_round_trips(store, tmp_path):
    p = exported(store, tmp_path / "e.enc")
    assert SQLiteStore().import_file(A, p, decrypt_with=KEY).imported == 0


def test_import_stream_reads_an_encrypted_stream(store, tmp_path):
    filled(store)
    p = exported(store, tmp_path / "e.enc")
    assert SQLiteStore().import_stream(A, io.BytesIO(open(p, "rb").read()), decrypt_with=KEY).imported == 3


def test_a_sharded_store_passes_the_options_through(tmp_path):
    src, dst = ShardedStore(tmp_path / "a"), ShardedStore(tmp_path / "b")
    src.add(A, SECRET)
    p = tmp_path / "e.enc"
    assert src.export_file(A, p, encrypt_with=KEY, context="c") == 1
    assert dst.import_file(A, p, decrypt_with=KEY, context="c").imported == 1
    assert dst.import_stream(A, io.BytesIO(open(p, "rb").read()), decrypt_with=KEY, context="c").skipped_duplicates == 1
    src.close()
    dst.close()


# ---------------------------------------------------------------- what the file looks like

def test_the_header_is_plain_json_and_the_lines_hide_the_text(store, tmp_path):
    filled(store)
    p = exported(store, tmp_path / "e.enc")
    data = open(p, "rb").read()
    header = json.loads(raw_lines(p)[0])
    assert set(header) == {"count", "file_id", "format", "kdf", "version"}
    assert header["version"] == 2 and header["count"] == 3 and header["kdf"] == "none"
    assert header["format"] == "ragleap-memory-export" and len(base64.b64decode(header["file_id"])) == 16
    assert SECRET.encode() not in data and b"acme" not in data and b"note number" not in data
    assert len(raw_lines(p)) == 4 and data.endswith(b"\n")


def test_a_passphrase_header_names_the_scrypt_settings_and_a_salt(store, tmp_path):
    filled(store)
    h = json.loads(raw_lines(exported(store, tmp_path / "e.enc", encrypt_with=None, passphrase=PASS))[0])
    assert h["kdf"] == "scrypt" and (h["log2_n"], h["r"], h["p"]) == (15, 8, 1)
    assert len(base64.b64decode(h["salt"])) == 16
    assert PASS not in json.dumps(h)


def test_two_exports_of_the_same_data_differ(store, tmp_path):
    filled(store)
    a = open(exported(store, tmp_path / "a.enc"), "rb").read()
    b = open(exported(store, tmp_path / "b.enc"), "rb").read()
    assert a != b
    assert json.loads(a.split(b"\n")[0])["file_id"] != json.loads(b.split(b"\n")[0])["file_id"]


def test_the_format_names_match_the_plain_reader():
    assert exportcrypto.FORMAT == sqlite_store.EXPORT_FORMAT
    assert exportcrypto.MAX_PLAIN_LINE_BYTES == sqlite_store.MAX_LINE_BYTES


# ---------------------------------------------------------------- the file on disk

def test_the_file_is_private_and_never_overwrites(store, tmp_path):
    filled(store)
    p = exported(store, tmp_path / "e.enc")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    with pytest.raises(FileExistsError):
        store.export_file(A, p, encrypt_with=KEY)


def test_a_failed_encrypted_export_leaves_no_file(store, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_export_encrypted_rows", lambda scope: [{"x": {1, 2}}])
    p = tmp_path / "e.enc"
    with pytest.raises(TypeError):
        store.export_file(A, p, encrypt_with=KEY)
    assert not p.exists()


@pytest.mark.parametrize("kw, message", [
    ({"encrypt_with": KEY, "passphrase": PASS}, "not both"),
    ({"context": "c"}, "context needs"),
    ({"encrypt_with": "not a key"}, "must be a Key or a KeyRing"),
    ({"passphrase": 12345678}, "must be a str or a SecretStr"),
    ({"encrypt_with": KEY, "context": 5}, "context must be str or bytes"),
    ({"passphrase": "short"}, "at least 8 characters"),
])
def test_bad_export_arguments_create_no_file(store, tmp_path, kw, message):
    p = tmp_path / "e.enc"
    with pytest.raises(InvalidEntryError, match=message):
        store.export_file(A, p, **kw)
    assert not p.exists()


def test_a_context_over_the_limit_is_refused_on_both_sides(store, tmp_path):
    with pytest.raises(LimitError):
        store.export_file(A, tmp_path / "e.enc", encrypt_with=KEY, context="x" * 1025)
    p = exported(store, tmp_path / "f.enc", context="x" * 1024)
    assert SQLiteStore().import_file(A, p, decrypt_with=KEY, context="x" * 1024).imported == 0
    with pytest.raises(LimitError):
        SQLiteStore().import_file(A, p, decrypt_with=KEY, context="x" * 1025)


# ---------------------------------------------------------------- tampering

def test_every_single_bit_changed_in_the_file_is_refused(store, tmp_path):
    filled(store, n=2)
    data = open(exported(store, tmp_path / "e.enc", context="c"), "rb").read()
    dst = SQLiteStore()
    survived = []
    for pos in range(len(data)):
        for bit in range(8):
            bad = bytearray(data)
            bad[pos] ^= 1 << bit
            try:
                dst.import_stream(A, io.BytesIO(bytes(bad)), decrypt_with=KEY, context="c")
                survived.append((pos, bit))
            except REFUSED:
                pass
    assert survived == []
    assert dst.count(A) == 0


def test_bit_changes_in_a_passphrase_file_are_refused(store, tmp_path):
    filled(store, n=2)
    data = open(exported(store, tmp_path / "e.enc", encrypt_with=None, passphrase=PASS), "rb").read()
    dst = SQLiteStore()
    for pos in list(range(0, len(data), 23)) + [len(data) - 1]:
        bad = bytearray(data)
        bad[pos] ^= 0x01
        with pytest.raises(REFUSED):
            dst.import_stream(A, io.BytesIO(bytes(bad)), passphrase=PASS)
    assert dst.count(A) == 0


def lines_of(store, tmp_path, n=3, name="e.enc"):
    filled(store, n=n)
    return raw_lines(exported(store, tmp_path / name))


def test_swapped_dropped_duplicated_and_extra_lines_are_refused(store, tmp_path):
    ls = lines_of(store, tmp_path)
    cases = {
        "swapped": [ls[0], ls[2], ls[1], ls[3]],
        "dropped": [ls[0], ls[1], ls[3]],
        "duplicated": [ls[0], ls[1], ls[1], ls[3]],
        "extra": [*ls, ls[1]],
        "header only": [ls[0]],
        "last cut": ls[:-1],
    }
    for name, lines in cases.items():
        dst = SQLiteStore()
        with pytest.raises(InvalidEntryError):
            dst.import_file(A, write_lines(tmp_path / "x.enc", lines), decrypt_with=KEY)
        assert dst.count(A) == 0, name
        os.remove(tmp_path / "x.enc")


def test_count_and_extra_line_messages_are_specific(store, tmp_path):
    ls = lines_of(store, tmp_path)
    with pytest.raises(InvalidEntryError, match="entry count does not match"):
        SQLiteStore().import_file(A, write_lines(tmp_path / "a", ls[:-1]), decrypt_with=KEY)
    with pytest.raises(InvalidEntryError, match="entry count does not match"):
        SQLiteStore().import_file(A, write_lines(tmp_path / "b", [*ls, ls[1]]), decrypt_with=KEY)


def test_lines_mixed_from_two_files_are_refused(store, tmp_path):
    one = lines_of(store, tmp_path, name="one.enc")
    two = raw_lines(exported(store, tmp_path / "two.enc"))
    with pytest.raises(InvalidEntryError, match="line 3: cannot decrypt"):
        SQLiteStore().import_file(A, write_lines(tmp_path / "m", [one[0], one[1], two[2], one[3]]), decrypt_with=KEY)


def test_a_changed_header_is_refused_even_when_it_means_the_same_thing(store, tmp_path):
    ls = lines_of(store, tmp_path)
    h = json.loads(ls[0])
    variants = [
        json.dumps(dict(reversed(list(h.items())))).encode(),  # other key order
        json.dumps(h, sort_keys=True, indent=1).replace("\n", " ").encode(),
        json.dumps(h, sort_keys=True, separators=(",", ":")).encode(),
        b" " + ls[0],
        ls[0] + b"\r",
        json.dumps({**h, "file_id": base64.b64encode(os.urandom(16)).decode()}, sort_keys=True).encode(),
    ]
    for v in variants:
        with pytest.raises(InvalidEntryError):
            SQLiteStore().import_file(A, write_lines(tmp_path / "h", [v, *ls[1:]]), decrypt_with=KEY)
        os.remove(tmp_path / "h")


def test_a_changed_count_is_refused(store, tmp_path):
    ls = lines_of(store, tmp_path)
    h = json.loads(ls[0])
    for count in (0, 2, 4):
        bad = json.dumps({**h, "count": count}, sort_keys=True).encode()
        with pytest.raises(InvalidEntryError):
            SQLiteStore().import_file(A, write_lines(tmp_path / "c", [bad, *ls[1:]]), decrypt_with=KEY)
        os.remove(tmp_path / "c")


def test_line_endings_and_padding_must_be_exact(store, tmp_path):
    ls = lines_of(store, tmp_path)
    for bad in (ls[1] + b"\r", b" " + ls[1], ls[1] + b" ", ls[1].replace(b"=", b""), b"%%%%", b""):
        lines = [ls[0], bad, *ls[2:]]
        if bad == b"":
            lines = [ls[0], ls[1], b"", *ls[2:]]   # a blank line is allowed, so this one is accepted
            assert SQLiteStore().import_file(A, write_lines(tmp_path / "ok", lines), decrypt_with=KEY).imported == 3
            os.remove(tmp_path / "ok")
            continue
        with pytest.raises(InvalidEntryError):
            SQLiteStore().import_file(A, write_lines(tmp_path / "e", lines), decrypt_with=KEY)
        os.remove(tmp_path / "e")


def test_a_missing_final_newline_is_refused(store, tmp_path):
    p = exported(filled(store), tmp_path / "e.enc")
    data = open(p, "rb").read()
    with pytest.raises(InvalidEntryError):
        SQLiteStore().import_stream(A, io.BytesIO(data[:-1]), decrypt_with=KEY)


def test_a_changed_base64_padding_bit_is_not_accepted(tmp_path):
    alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    done = 0
    for k in range(3):                                           # the three possible padding lengths
        ls = raw_lines(forge(tmp_path / f"f{k}", [entry(text="t" * k)]))
        body = ls[1]
        if b"=" not in body:
            continue
        i = body.index(b"=")
        changed = body[:i - 1] + alphabet[alphabet.index(body[i - 1:i]) ^ 1:][:1] + body[i:]
        assert changed != body
        # Python's own decoder would accept this line and give the same bytes back:
        assert base64.b64decode(changed, validate=True) == base64.b64decode(body, validate=True)
        with pytest.raises(InvalidEntryError, match="line 2: cannot decrypt"):
            SQLiteStore().import_file(A, write_lines(tmp_path / f"p{k}", [ls[0], changed]), decrypt_with=KEY)
        done += 1
    assert done >= 1


# ---------------------------------------------------------------- no downgrade, modes

def test_modes_cannot_be_mixed(store, tmp_path):
    filled(store)
    enc = exported(store, tmp_path / "e.enc")
    pas = exported(store, tmp_path / "p.enc", encrypt_with=None, passphrase=PASS)
    plain = tmp_path / "plain.jsonl"
    store.export_file(A, plain)
    dst = SQLiteStore()
    with pytest.raises(InvalidEntryError, match="this export file is encrypted"):
        dst.import_file(A, enc)
    with pytest.raises(InvalidEntryError, match="this export file is encrypted"):
        dst.import_file(A, pas)
    with pytest.raises(InvalidEntryError, match="plain file is refused"):
        dst.import_file(A, plain, decrypt_with=KEY)
    with pytest.raises(InvalidEntryError, match="plain file is refused"):
        dst.import_file(A, plain, passphrase=PASS)
    with pytest.raises(InvalidEntryError, match="encrypted with a key"):
        dst.import_file(A, enc, passphrase=PASS)
    with pytest.raises(InvalidEntryError, match="encrypted with a passphrase"):
        dst.import_file(A, pas, decrypt_with=KEY)
    with pytest.raises(InvalidEntryError, match="not both"):
        dst.import_file(A, enc, decrypt_with=KEY, passphrase=PASS)
    with pytest.raises(InvalidEntryError, match="context needs"):
        dst.import_file(A, plain, context="c")
    with pytest.raises(InvalidEntryError, match="must be a Key or a KeyRing"):
        dst.import_file(A, enc, decrypt_with="a string")
    assert dst.count(A) == 0
    assert dst.import_file(A, plain).imported == 3         # a plain 0.3.0 style file still imports as before


def test_garbage_is_refused_whichever_arguments_are_given(tmp_path):
    p = tmp_path / "g"
    p.write_bytes(b"not json at all\n")
    for kw in ({}, {"decrypt_with": KEY}, {"passphrase": PASS}):
        with pytest.raises(InvalidEntryError):
            SQLiteStore().import_file(A, p, **kw)


def test_wrong_passphrase_and_wrong_key_say_the_same_thing_without_secrets(store, tmp_path):
    filled(store)
    pas = exported(store, tmp_path / "p.enc", encrypt_with=None, passphrase=PASS)
    enc = exported(store, tmp_path / "e.enc")
    other = "other-" + os.urandom(6).hex()
    msgs = []
    for path, kw in ((pas, {"passphrase": other}), (enc, {"decrypt_with": Key.generate("k1")})):
        with pytest.raises(InvalidEntryError) as ei:
            SQLiteStore().import_file(A, path, **kw)
        msgs.append(str(ei.value))
    assert msgs == ["line 2: cannot decrypt"] * 2
    assert all(SECRET not in m and PASS not in m and other not in m for m in msgs)


def test_a_short_passphrase_is_refused_on_import_too(store, tmp_path):
    p = exported(filled(store), tmp_path / "p.enc", encrypt_with=None, passphrase=PASS)
    with pytest.raises(InvalidEntryError, match="at least 8 characters"):
        SQLiteStore().import_file(A, p, passphrase="short")


# ---------------------------------------------------------------- hostile headers

def with_header(path, header, ls):
    return write_lines(path, [json.dumps(header, sort_keys=True).encode(), *ls[1:]])


def test_hostile_scrypt_settings_and_fields_in_a_header_are_refused(store, tmp_path):
    filled(store)
    ls = raw_lines(exported(store, tmp_path / "p.enc", encrypt_with=None, passphrase=PASS))
    h = json.loads(ls[0])
    weak = "outside the allowed range"
    bad = [({"log2_n": 30}, weak), ({"log2_n": 3}, weak), ({"r": 1000}, weak), ({"p": 99}, weak),
           ({"log2_n": True}, "must be an int"), ({"log2_n": "15"}, "must be an int"),
           ({"salt": base64.b64encode(b"short").decode()}, "supported version"),
           ({"salt": "not base64!!"}, "supported version"), ({"salt": 5}, "supported version"),
           ({"file_id": base64.b64encode(b"short").decode()}, "supported version"),
           ({"file_id": None}, "supported version"), ({"kdf": "md5"}, "supported version"),
           ({"count": -1}, "supported version"), ({"count": True}, "supported version"),
           ({"count": "3"}, "supported version"), ({"count": 1.5}, "supported version")]
    for change, message in bad:
        with pytest.raises(InvalidEntryError, match=message):
            SQLiteStore().import_file(A, with_header(tmp_path / "h", {**h, **change}, ls), passphrase=PASS)
        os.remove(tmp_path / "h")


def test_a_header_with_a_non_canonical_salt_or_file_id_is_refused(store, tmp_path):
    filled(store)
    ls = raw_lines(exported(store, tmp_path / "p.enc", encrypt_with=None, passphrase=PASS))
    h = json.loads(ls[0])
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

    def spare_bit_changed(text):      # same bytes for Python's own decoder, but not the canonical text
        i = text.index("=")
        return text[:i - 1] + alphabet[alphabet.index(text[i - 1]) ^ 1] + text[i:]
    for field in ("salt", "file_id"):
        assert base64.b64decode(spare_bit_changed(h[field])) == base64.b64decode(h[field])
    for change in ({"salt": spare_bit_changed(h["salt"])}, {"file_id": spare_bit_changed(h["file_id"])},
                   {"salt": " " + h["salt"]}, {"file_id": " " + h["file_id"]}):
        with pytest.raises(InvalidEntryError, match="supported version"):
            SQLiteStore().import_file(A, with_header(tmp_path / "h", {**h, **change}, ls), passphrase=PASS)
        os.remove(tmp_path / "h")


def test_a_header_of_another_format_or_version_is_not_read_as_encrypted(store, tmp_path):
    ls = lines_of(store, tmp_path)
    h = json.loads(ls[0])
    for change in ({"version": 3}, {"version": 1}, {"version": "2"}, {"version": True}, {"version": None},
                   {"format": "other"}, {"format": None}):
        with pytest.raises(InvalidEntryError, match="not an encrypted ragleap-memory export file"):
            SQLiteStore().import_file(A, with_header(tmp_path / "v", {**h, **change}, ls), decrypt_with=KEY)
        os.remove(tmp_path / "v")


# ---------------------------------------------------------------- limits

def test_limits_apply_to_the_file_and_to_the_decrypted_data(store, tmp_path):
    ls = lines_of(store, tmp_path, n=3)
    p = tmp_path / "e.enc"
    disk = os.path.getsize(p)
    plain = sum(len(json.dumps(e, sort_keys=True)) + 1 for e in store.export(A))
    assert disk <= 2 * (plain - 1)
    with pytest.raises(LimitError, match="file has more than 2 entries"):
        SQLiteStore().import_file(A, p, decrypt_with=KEY, max_entries=2)
    with pytest.raises(LimitError, match=f"file is larger than {plain - 1} bytes"):
        SQLiteStore().import_file(A, p, decrypt_with=KEY, max_bytes=plain - 1)
    with pytest.raises(LimitError, match=f"file is larger than {2 * (disk // 2 - 1)} bytes"):
        SQLiteStore().import_file(A, p, decrypt_with=KEY, max_bytes=disk // 2 - 1)
    assert SQLiteStore().import_file(A, p, decrypt_with=KEY, max_bytes=plain + 100, max_entries=3).imported == 3


def test_an_oversize_encrypted_line_is_refused(store, tmp_path):
    ls = lines_of(store, tmp_path, n=1)
    huge = b"A" * 90_001
    with pytest.raises(LimitError, match="line 2 is longer than 90000 bytes"):
        SQLiteStore().import_file(A, write_lines(tmp_path / "big", [ls[0], huge]), decrypt_with=KEY)


def test_a_decrypted_line_over_the_plain_limit_is_refused(tmp_path):
    p = forge(tmp_path / "f", [entry(text="x" * 66_000)])
    with pytest.raises(LimitError, match="line 2 is longer than 65536 bytes"):
        SQLiteStore().import_file(A, p, decrypt_with=KEY)


def test_a_decrypted_line_with_a_newline_inside_is_refused(tmp_path):
    plan = exportcrypto.prepare_export(1, KEY, None, None)
    header_hash = hashlib.sha256(plan.header_line[:-1]).digest()
    env = crypto.encrypt(KEY, b'{"scope": "tenant=acme"}\n{"x": 1}', exportcrypto._ctx(header_hash, 1, b""))
    p = write_lines(tmp_path / "n", [plan.header_line[:-1], base64.b64encode(env)])
    with pytest.raises(InvalidEntryError, match="line 2: not valid JSON"):
        SQLiteStore().import_file(A, p, decrypt_with=KEY)


# ---------------------------------------------------------------- import stays the single safe path

def test_blank_lines_are_tolerated_and_line_numbers_stay_true(tmp_path):
    p = forge(tmp_path / "f", [entry(text="fine"), entry(text="bad", kind="nonsense")])
    ls = raw_lines(p)
    q = write_lines(tmp_path / "g", [b"", ls[0], b"", ls[1], b"", ls[2]])
    with pytest.raises(InvalidEntryError, match="line 6: .*") as ei:
        SQLiteStore().import_file(A, q, decrypt_with=KEY)
    assert "bad" not in str(ei.value)
    ok = write_lines(tmp_path / "h", [b"", ls[0], b"", ls[1]])
    # count says 2 but only one entry line is present: refused
    with pytest.raises(InvalidEntryError, match="entry count"):
        SQLiteStore().import_file(A, ok, decrypt_with=KEY)


def test_trust_is_still_capped_after_decrypting(tmp_path):
    p = forge(tmp_path / "f", [entry(text="a1", trust="owner"), entry(text="a2", trust="model")])
    s = SQLiteStore()
    s.import_file(A, p, decrypt_with=KEY)
    assert {e["trust"] for e in s.export(A)} == {"untrusted"}
    s2 = SQLiteStore()
    s2.import_file(A, p, decrypt_with=KEY, max_trust="model")
    assert {e["text"]: e["trust"] for e in s2.export(A)} == {"a1": "model", "a2": "model"}


def test_scope_quota_and_one_transaction_still_apply(tmp_path):
    other = forge(tmp_path / "o", [entry(text="mine"), entry(scope="tenant=globex", text="theirs")])
    s = SQLiteStore()
    with pytest.raises(InvalidEntryError):
        s.import_file(A, other, decrypt_with=KEY)
    assert s.count(A) == 0 and s.count(B) == 0
    two = forge(tmp_path / "t", [entry(text="one"), entry(text="two")])
    q = SQLiteStore(quota_entries=1)
    with pytest.raises(QuotaExceededError):
        q.import_file(A, two, decrypt_with=KEY)
    assert q.count(A) == 0
    d = SQLiteStore()
    d.import_file(A, two, decrypt_with=KEY)
    assert d.import_file(A, two, decrypt_with=KEY).skipped_duplicates == 2


def test_a_bad_entry_imports_nothing_and_the_error_has_no_text(tmp_path):
    p = forge(tmp_path / "f", [entry(text="fine one"), entry(text=SECRET, kind="nonsense"), entry(text="fine three")])
    s = SQLiteStore()
    with pytest.raises(InvalidEntryError) as ei:
        s.import_file(A, p, decrypt_with=KEY)
    assert "line 3" in str(ei.value) and SECRET not in str(ei.value)
    assert s.count(A) == 0


# ---------------------------------------------------------------- audit

def test_audit_events_say_encrypted_and_never_hold_secrets(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    s = SQLiteStore(audit=AuditLog(path))
    s.add(A, SECRET)
    p = tmp_path / "e.enc"
    s.export_file(A, p, encrypt_with=KEY, context="ctx-secret")
    export_event = json.loads(open(path).read().splitlines()[-1])
    assert export_event["event"] == "memory.export" and export_event["data"] == {"count": 1, "encrypted": True}
    d = SQLiteStore(audit=AuditLog(path))
    d.import_file(A, p, decrypt_with=KEY, context="ctx-secret")
    import_event = json.loads(open(path).read().splitlines()[-1])
    assert import_event["event"] == "memory.import"
    assert import_event["data"] == {"imported": 1, "skipped": 0, "encrypted": True}
    plain = tmp_path / "p.jsonl"
    s.export_file(A, plain)
    assert json.loads(open(path).read().splitlines()[-1])["data"] == {"count": 1}
    SQLiteStore(audit=AuditLog(path)).import_file(A, plain)
    assert json.loads(open(path).read().splitlines()[-1])["data"] == {"imported": 1, "skipped": 0}
    pas = tmp_path / "pp.enc"
    s.export_file(A, pas, passphrase=PASS)
    log = open(path).read()
    assert verify(path).ok
    for secret in (SECRET, KEY.reveal_b64(), PASS, "ctx-secret"):
        assert secret not in log


def test_a_refused_import_is_not_audited(store, tmp_path):
    path = str(tmp_path / "audit.jsonl")
    s = SQLiteStore(audit=AuditLog(path))
    p = exported(filled(store), tmp_path / "e.enc")
    with pytest.raises(InvalidEntryError):
        s.import_file(A, p, decrypt_with=Key.generate("k1"))
    assert not os.path.exists(path) or verify(path).records == 0


# ---------------------------------------------------------------- what each line is bound to

def forged_with_context(tmp_path, context_bytes):
    plan = exportcrypto.prepare_export(1, KEY, None, None)
    env = crypto.encrypt(KEY, json.dumps(entry(), sort_keys=True).encode(), context_bytes(plan))
    return write_lines(tmp_path / "f", [plan.header_line[:-1], base64.b64encode(env)])


def test_a_line_is_bound_to_the_label_the_header_the_position_and_the_context(tmp_path):
    def right(plan, i=1, user=b"", label=exportcrypto.LABEL, hashed=True):
        h = hashlib.sha256(plan.header_line[:-1]).digest() if hashed else b""
        return label + h + struct.pack(">Q", i) + user

    # a control: built by hand the way the module builds it, the line is accepted
    assert SQLiteStore().import_file(A, forged_with_context(tmp_path, right), decrypt_with=KEY).imported == 1
    os.remove(tmp_path / "f")
    wrong = {
        "no label": lambda plan: right(plan, label=b""),
        "other label": lambda plan: right(plan, label=b"ragleap-memory-export/3"),
        "no header hash": lambda plan: right(plan, hashed=False),
        "other position": lambda plan: right(plan, i=2),
        "extra context": lambda plan: right(plan, user=b"x"),
    }
    for name, context_bytes in wrong.items():
        with pytest.raises(InvalidEntryError, match="line 2: cannot decrypt"):
            SQLiteStore().import_file(A, forged_with_context(tmp_path, context_bytes), decrypt_with=KEY)
        os.remove(tmp_path / "f")


# ---------------------------------------------------------------- more checks that keep the rules honest

def test_too_many_entries_is_refused_before_anything_is_decrypted(store, tmp_path, monkeypatch):
    p = exported(filled(store), tmp_path / "e.enc")

    def boom(*a, **k):
        raise AssertionError("decrypt was called")
    monkeypatch.setattr(crypto, "decrypt", boom)
    with pytest.raises(LimitError, match="file has more than 2 entries"):
        SQLiteStore().import_file(A, p, decrypt_with=KEY, max_entries=2)


def test_an_empty_export_without_its_final_newline_is_refused(store, tmp_path):
    data = open(exported(store, tmp_path / "e.enc"), "rb").read()
    assert data.endswith(b"\n")
    with pytest.raises(InvalidEntryError):
        SQLiteStore().import_stream(A, io.BytesIO(data[:-1]), decrypt_with=KEY)


def test_an_encrypted_export_holds_only_the_scope_asked_for(store, tmp_path):
    store.add(A, "mine")
    store.add(B, "theirs")
    p = exported(store, tmp_path / "e.enc")
    dst = SQLiteStore()
    assert dst.import_file(A, p, decrypt_with=KEY).imported == 1     # an entry outside A would stop the import
    assert texts(dst, A) == ["mine"] and dst.count(B) == 0


def test_a_flood_of_blank_lines_is_cut_off(tmp_path):
    class Blanks:
        reads = 0

        def readline(self, limit=-1):
            Blanks.reads += 1
            assert Blanks.reads < 10_000, "kept reading blank lines past the limit"
            return b"\n"
    with pytest.raises(LimitError, match="file is larger than 100 bytes"):
        SQLiteStore().import_stream(A, Blanks(), max_bytes=100)
    assert Blanks.reads < 1_000


def test_the_salt_and_file_id_are_new_for_every_passphrase_export(store, tmp_path):
    filled(store)
    a = json.loads(raw_lines(exported(store, tmp_path / "a.enc", encrypt_with=None, passphrase=PASS))[0])
    b = json.loads(raw_lines(exported(store, tmp_path / "b.enc", encrypt_with=None, passphrase=PASS))[0])
    assert a["salt"] != b["salt"] and a["file_id"] != b["file_id"]


def test_a_passphrase_is_stretched_once_per_file_not_per_line(store, tmp_path, monkeypatch):
    import ragleap_security.keys as keys
    calls = []
    real = keys.derive_scrypt
    monkeypatch.setattr(keys, "derive_scrypt", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    filled(store, n=5)
    p = exported(store, tmp_path / "e.enc", encrypt_with=None, passphrase=PASS)
    assert len(calls) == 1
    SQLiteStore().import_file(A, p, passphrase=PASS)
    assert len(calls) == 2


def test_a_first_line_that_is_not_an_object_is_refused(tmp_path):
    for first in (b"[1, 2, 3]", b'"text"', b"42", b"null"):
        p = write_lines(tmp_path / "x", [first])
        for kw in ({}, {"decrypt_with": KEY}):
            with pytest.raises(InvalidEntryError):
                SQLiteStore().import_file(A, p, **kw)
        os.remove(tmp_path / "x")


def test_a_leading_blank_line_before_an_encrypted_header_is_fine(store, tmp_path):
    ls = lines_of(store, tmp_path)
    p = write_lines(tmp_path / "b", [b"", b"  ", *ls])
    assert SQLiteStore().import_file(A, p, decrypt_with=KEY).imported == 3
