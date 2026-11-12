"""Without the optional cryptography package nothing changes, and asking for encryption gives a clear message."""
import json
import sys

import pytest

import ragleap_security
from ragleap_memory import InvalidEntryError, MemoryStoreError, SQLiteStore
from ragleap_security import Key, Scope

A = Scope("acme")
NEEDS = r"pip install ragleap-memory\[crypto\]"


@pytest.fixture
def no_crypto(monkeypatch):
    """Make `import ragleap_security.crypto` fail, as it does when cryptography is not installed."""
    monkeypatch.delattr(ragleap_security, "crypto", raising=False)
    monkeypatch.setitem(sys.modules, "ragleap_security.crypto", None)


def encrypted_header_file(path, count=0):
    header = {"count": count, "file_id": "AAAAAAAAAAAAAAAAAAAAAA==", "format": "ragleap-memory-export",
              "kdf": "none", "version": 2}
    path.write_text(json.dumps(header, sort_keys=True) + "\n")
    return path


def test_plain_export_and_import_work_without_the_package(no_crypto, tmp_path):
    s = SQLiteStore()
    s.add(A, "hello there")
    p = tmp_path / "plain.jsonl"
    assert s.export_file(A, p) == 1
    d = SQLiteStore()
    assert d.import_file(A, p).imported == 1


def test_asking_for_encryption_says_what_to_install(no_crypto, tmp_path):
    s = SQLiteStore()
    for kw in ({"encrypt_with": Key.generate("k1")}, {"passphrase": "a long enough phrase"}):
        p = tmp_path / "e.enc"
        with pytest.raises(MemoryStoreError, match=NEEDS):
            s.export_file(A, p, **kw)
        assert not p.exists()


def test_decrypting_says_what_to_install(no_crypto, tmp_path):
    p = encrypted_header_file(tmp_path / "e.enc")
    with pytest.raises(MemoryStoreError, match=NEEDS):
        SQLiteStore().import_file(A, p, decrypt_with=Key.generate("k1"))
    with pytest.raises(MemoryStoreError, match=NEEDS):
        SQLiteStore().import_file(A, p, passphrase="a long enough phrase")


def test_an_encrypted_file_is_still_refused_without_a_key(no_crypto, tmp_path):
    with pytest.raises(InvalidEntryError, match="this export file is encrypted"):
        SQLiteStore().import_file(A, encrypted_header_file(tmp_path / "e.enc"))
