import hashlib
import os
import time

import pytest

from ragleap_voice import api


def make(tmp_path, data=b"model bytes"):
    d = tmp_path / "m"
    d.mkdir()
    (d / "a.bin").write_bytes(data)
    lock = {"files": [{"path": "a.bin", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}
    return lock, d


@pytest.fixture
def counter(monkeypatch):
    api._VERIFIED.clear()
    monkeypatch.setattr(api, "_RACY_NS", 0)  # tests write and check within milliseconds
    monkeypatch.delenv("RAGLEAP_VOICE_VERIFY", raising=False)
    calls = []
    real = api.verify_dir

    def counting(lock, d):
        calls.append(str(d))
        return real(lock, d)
    monkeypatch.setattr(api, "verify_dir", counting)
    return calls


def test_second_call_does_not_rehash(tmp_path, counter):
    lock, d = make(tmp_path)
    assert api._verify_cached(lock, d) == []
    assert api._verify_cached(lock, d) == []
    assert api._verify_cached(lock, d) == []
    assert len(counter) == 1


def test_changed_file_is_rehashed_and_caught(tmp_path, counter):
    lock, d = make(tmp_path)
    assert api._verify_cached(lock, d) == []
    time.sleep(0.05)  # cross the filesystem timestamp granularity
    (d / "a.bin").write_bytes(b"MODEL bytes")  # same size, different content
    problems = api._verify_cached(lock, d)
    assert len(counter) == 2 and any("sha256 mismatch" in p for p in problems)


def test_restored_content_after_tamper_still_rehashes(tmp_path, counter):
    lock, d = make(tmp_path)
    api._verify_cached(lock, d)
    time.sleep(0.05)
    (d / "a.bin").write_bytes(b"MODEL bytes")
    time.sleep(0.05)
    (d / "a.bin").write_bytes(b"model bytes")  # content restored, ctime changed
    assert api._verify_cached(lock, d) == []
    assert len(counter) == 2


def test_extra_file_is_caught(tmp_path, counter):
    lock, d = make(tmp_path)
    api._verify_cached(lock, d)
    (d / "evil.py").write_text("x")
    assert any("unexpected file" in p for p in api._verify_cached(lock, d))
    assert len(counter) == 2


def test_failed_check_is_never_cached(tmp_path, counter):
    lock, d = make(tmp_path, b"abc")
    (d / "a.bin").write_bytes(b"xyz")
    assert api._verify_cached(lock, d) != []
    assert api._verify_cached(lock, d) != []
    assert len(counter) == 2 and not api._VERIFIED


def test_env_override_hashes_every_call(tmp_path, counter, monkeypatch):
    monkeypatch.setenv("RAGLEAP_VOICE_VERIFY", "always")
    lock, d = make(tmp_path)
    api._verify_cached(lock, d)
    api._verify_cached(lock, d)
    assert len(counter) == 2 and not api._VERIFIED


def test_missing_folder_is_reported(tmp_path, counter):
    lock, d = make(tmp_path)
    problems = api._verify_cached(lock, tmp_path / "nope")
    assert problems and not api._VERIFIED


def test_symlink_swap_is_caught(tmp_path, counter):
    lock, d = make(tmp_path)
    api._verify_cached(lock, d)
    os.remove(d / "a.bin")
    os.symlink(tmp_path / "elsewhere", d / "a.bin")
    assert api._verify_cached(lock, d) != []


def test_freshly_written_folder_is_not_cached(tmp_path, counter, monkeypatch):
    monkeypatch.setattr(api, "_RACY_NS", 2_000_000_000)
    lock, d = make(tmp_path)
    assert api._verify_cached(lock, d) == []
    assert api._verify_cached(lock, d) == []
    assert len(counter) == 2 and not api._VERIFIED


def test_fingerprint_includes_device(tmp_path):
    lock, d = make(tmp_path)
    fp = api._fingerprint(d)
    assert fp and all(len(e) == 7 and e[6] == os.lstat(d).st_dev for e in fp)


def test_write_through_a_hard_link_is_caught(tmp_path, counter):
    lock, d = make(tmp_path)
    other = tmp_path / "other-name"
    os.link(d / "a.bin", other)
    assert api._verify_cached(lock, d) == []
    time.sleep(0.05)
    with open(other, "r+b") as f:
        f.write(b"MODEL bytes")
    problems = api._verify_cached(lock, d)
    assert len(counter) == 2 and any("sha256 mismatch" in p for p in problems)


def test_swapped_parent_folder_is_caught(tmp_path, counter):
    lock, d = make(tmp_path)
    assert api._verify_cached(lock, d) == []
    time.sleep(0.05)
    os.rename(d, tmp_path / "old")
    d.mkdir()
    (d / "a.bin").write_bytes(b"MODEL bytes")
    problems = api._verify_cached(lock, d)
    assert len(counter) == 2 and any("sha256 mismatch" in p for p in problems)
