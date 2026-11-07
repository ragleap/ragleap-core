import hashlib
import json
import os

import pytest

from ragleap_security import (IntegrityError, Key, LimitError, Manifest, build_manifest, file_digest,
                              verify_manifest)
from ragleap_security import integrity


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "app"
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"alpha")
    (root / "sub" / "b.txt").write_bytes(b"beta")
    (root / "sub" / "deep" / "c.bin").write_bytes(bytes(range(256)))
    return root


def test_file_digest_matches_sha256_and_chunks(tmp_path):
    p = tmp_path / "f"
    data = os.urandom(3 * (1 << 20) + 17)
    p.write_bytes(data)
    assert file_digest(p) == hashlib.sha256(data).hexdigest()
    p.write_bytes(b"")
    assert file_digest(p) == hashlib.sha256(b"").hexdigest()


def test_file_digest_size_limit_boundary(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x" * 10)
    assert file_digest(p, max_bytes=10)
    with pytest.raises(LimitError):
        file_digest(p, max_bytes=9)
    with pytest.raises(ValueError):
        file_digest(p, max_bytes=-1)
    with pytest.raises(ValueError):
        file_digest(p, max_bytes=True)


def test_file_digest_refuses_symlinks_directories_and_missing(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x")
    (tmp_path / "link").symlink_to(p)
    with pytest.raises(IntegrityError):
        file_digest(tmp_path / "link")
    with pytest.raises(IntegrityError):
        file_digest(tmp_path)
    with pytest.raises(IntegrityError):
        file_digest(tmp_path / "nope")


def test_manifest_lists_every_file_with_posix_paths(tree):
    m = build_manifest(tree)
    assert set(m.files) == {"a.txt", "sub/b.txt", "sub/deep/c.bin"}
    assert m.files["a.txt"] == hashlib.sha256(b"alpha").hexdigest() and m.mac is None
    assert list(m.files) == sorted(m.files)


def test_untouched_tree_verifies(tree):
    r = verify_manifest(tree, build_manifest(tree))
    assert r.ok and r.changed == r.missing == r.extra == () and not r.mac_checked and r.error is None


def test_changed_missing_and_extra_are_reported(tree):
    m = build_manifest(tree)
    (tree / "a.txt").write_bytes(b"ALPHA")
    (tree / "sub" / "b.txt").unlink()
    (tree / "sub" / "new.txt").write_bytes(b"n")
    r = verify_manifest(tree, m)
    assert not r.ok and r.changed == ("a.txt",) and r.missing == ("sub/b.txt",) and r.extra == ("sub/new.txt",)


def test_one_changed_byte_is_enough(tree):
    m = build_manifest(tree)
    data = bytearray((tree / "sub" / "deep" / "c.bin").read_bytes())
    data[100] ^= 1
    (tree / "sub" / "deep" / "c.bin").write_bytes(bytes(data))
    assert verify_manifest(tree, m).changed == ("sub/deep/c.bin",)


def test_build_refuses_symlinks_and_special_files(tree):
    (tree / "ln").symlink_to(tree / "a.txt")
    with pytest.raises(IntegrityError) as e:
        build_manifest(tree)
    assert "ln" in str(e.value)
    (tree / "ln").unlink()
    (tree / "dl").symlink_to(tree / "sub")
    with pytest.raises(IntegrityError):
        build_manifest(tree)
    (tree / "dl").unlink()
    os.mkfifo(tree / "pipe")
    with pytest.raises(IntegrityError):
        build_manifest(tree)


def test_symlink_added_later_is_extra_and_replacing_a_file_is_changed(tree):
    m = build_manifest(tree)
    (tree / "ln").symlink_to(tree / "a.txt")
    assert verify_manifest(tree, m).extra == ("ln",)
    (tree / "ln").unlink()
    (tree / "a.txt").unlink()
    (tree / "a.txt").symlink_to(tree / "sub" / "b.txt")
    r = verify_manifest(tree, m)
    assert r.changed == ("a.txt",) and r.missing == () and not r.ok


def test_symlinked_directory_is_never_followed(tree, tmp_path):
    m = build_manifest(tree)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"s")
    (tree / "dl").symlink_to(outside)
    r = verify_manifest(tree, m)
    assert r.extra == ("dl",) and "dl/secret.txt" not in r.extra


def test_exclude_names_skip_files_and_directories_anywhere(tree):
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "x.pyc").write_bytes(b"1")
    (tree / "sub" / "__pycache__").mkdir()
    (tree / "sub" / "__pycache__" / "y.pyc").write_bytes(b"2")
    (tree / "notes.tmp").write_bytes(b"t")
    m = build_manifest(tree, exclude=["__pycache__", "notes.tmp"])
    assert set(m.files) == {"a.txt", "sub/b.txt", "sub/deep/c.bin"}
    assert verify_manifest(tree, m, exclude=["__pycache__", "notes.tmp"]).ok
    assert verify_manifest(tree, m).extra == ("__pycache__/x.pyc", "notes.tmp", "sub/__pycache__/y.pyc")


def test_max_files_boundary(tree):
    assert build_manifest(tree, max_files=3)
    with pytest.raises(IntegrityError):
        build_manifest(tree, max_files=2)
    r = verify_manifest(tree, build_manifest(tree), max_files=2)
    assert not r.ok and r.error == "too many files"


def test_per_file_size_limit_makes_a_file_changed(tree):
    m = build_manifest(tree)
    r = verify_manifest(tree, m, max_file_bytes=4)
    assert not r.ok and "a.txt" in r.changed
    with pytest.raises(LimitError):
        build_manifest(tree, max_file_bytes=4)


def test_root_must_be_a_directory(tree):
    with pytest.raises(IntegrityError, match="root is not a directory"):
        build_manifest(tree / "a.txt")
    r = verify_manifest(tree / "nope", Manifest(files={}))
    assert not r.ok and r.error == "root is not a directory"


def test_empty_directory_verifies_against_empty_manifest(tmp_path):
    d = tmp_path / "e"
    d.mkdir()
    m = build_manifest(d)
    assert m.files == {} and verify_manifest(d, m).ok


def test_signed_manifest_round_trip(tree):
    k = Key.generate("m")
    m = build_manifest(tree, key=k)
    assert m.mac and len(m.mac) == 64
    r = verify_manifest(tree, m, key=k)
    assert r.ok and r.mac_checked


def test_wrong_key_unsigned_and_edited_manifests_fail_with_a_key(tree):
    k = Key.generate("m")
    m = build_manifest(tree, key=k)
    assert verify_manifest(tree, m, key=Key.generate("m")).error == "manifest signature does not match"
    assert verify_manifest(tree, build_manifest(tree), key=k).error == "manifest is not signed"
    forged_files = dict(m.files)
    forged_files["a.txt"] = hashlib.sha256(b"evil").hexdigest()
    (tree / "a.txt").write_bytes(b"evil")
    r = verify_manifest(tree, Manifest(files=forged_files, mac=m.mac), key=k)
    assert not r.ok and r.mac_checked and r.error == "manifest signature does not match"
    dropped = {p: d for p, d in m.files.items() if p != "a.txt"}
    assert not verify_manifest(tree, Manifest(files=dropped, mac=m.mac), key=k).ok


def test_mac_covers_every_path_and_digest(tree):
    k = Key.generate("m")
    m = build_manifest(tree, key=k)
    renamed = {("z" + p): d for p, d in m.files.items()}
    assert verify_manifest(tree, Manifest(files=renamed, mac=m.mac), key=k).error
    assert integrity._mac(m.files, k) == m.mac != integrity._mac(m.files, Key.generate("m"))
    assert integrity._mac(m.files, k) != hashlib.sha256(integrity._canonical(m.files)).hexdigest()


def test_mac_uses_a_derived_key_not_the_raw_key(tree):
    import hmac
    k = Key.generate("m")
    m = build_manifest(tree, key=k)
    raw = hmac.new(k._material(), integrity._canonical(m.files), hashlib.sha256).hexdigest()
    assert m.mac != raw


def test_unsigned_manifest_ignores_a_signature_when_no_key_is_given(tree):
    m = build_manifest(tree, key=Key.generate("m"))
    r = verify_manifest(tree, m)
    assert r.ok and not r.mac_checked


def test_type_checks(tree):
    with pytest.raises(TypeError):
        verify_manifest(tree, {"a.txt": "x"})
    with pytest.raises(TypeError):
        verify_manifest(tree, build_manifest(tree), key="not a key")


def test_json_round_trip_is_canonical(tree):
    k = Key.generate("m")
    for m in (build_manifest(tree), build_manifest(tree, key=k)):
        text = m.to_json()
        assert Manifest.from_json(text) == m and Manifest.from_json(text.encode()) == m
        assert text == json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
    assert json.loads(build_manifest(tree).to_json())["mac"] is None


def _good():
    return {"format": "ragleap-security-manifest", "version": 1, "files": {"a": "0" * 64}, "mac": None}


def test_from_json_accepts_a_good_manifest():
    assert Manifest.from_json(json.dumps(_good())).files == {"a": "0" * 64}
    g = _good()
    g["mac"] = "f" * 64
    assert Manifest.from_json(json.dumps(g)).mac == "f" * 64


@pytest.mark.parametrize("edit", [
    lambda g: g.update(format="other"),
    lambda g: g.update(version=2),
    lambda g: g.update(version=True),
    lambda g: g.update(extra=1),
    lambda g: g.pop("mac"),
    lambda g: g.update(files=[]),
    lambda g: g.update(files={"a": "G" * 64}),
    lambda g: g.update(files={"a": "0" * 65}),
    lambda g: g.update(files={"a": "0" * 63}),
    lambda g: g.update(files={"a": "A" * 64}),
    lambda g: g.update(files={"a": 5}),
    lambda g: g.update(files={"/etc/passwd": "0" * 64}),
    lambda g: g.update(files={"../x": "0" * 64}),
    lambda g: g.update(files={"a/../x": "0" * 64}),
    lambda g: g.update(files={"a//x": "0" * 64}),
    lambda g: g.update(files={"./x": "0" * 64}),
    lambda g: g.update(files={"": "0" * 64}),
    lambda g: g.update(files={"a\x00b": "0" * 64}),
    lambda g: g.update(files={"a/": "0" * 64}),
    lambda g: g.update(mac="zz"),
    lambda g: g.update(mac="f" * 65),
    lambda g: g.update(mac="F" * 64),
    lambda g: g.update(mac=5),
])
def test_from_json_rejects_bad_manifests(edit):
    g = _good()
    edit(g)
    with pytest.raises(IntegrityError):
        Manifest.from_json(json.dumps(g))


@pytest.mark.parametrize("text", ["", "not json", "[]", "5", "null", "[" * 100000 + "]" * 100000])
def test_from_json_rejects_garbage_and_hostile_nesting(text):
    with pytest.raises((IntegrityError, LimitError)):
        Manifest.from_json(text)


def test_from_json_size_and_file_count_limits(monkeypatch):
    monkeypatch.setattr(integrity, "MAX_MANIFEST_BYTES", 10)
    with pytest.raises(LimitError):
        Manifest.from_json(json.dumps(_good()))
    monkeypatch.undo()
    g = _good()
    g["files"] = {f"f{i}": "0" * 64 for i in range(integrity.DEFAULT_MAX_FILES + 1)}
    with pytest.raises(IntegrityError):
        Manifest.from_json(json.dumps(g))
    g["files"] = {f"f{i}": "0" * 64 for i in range(integrity.DEFAULT_MAX_FILES)}
    assert len(Manifest.from_json(json.dumps(g)).files) == integrity.DEFAULT_MAX_FILES


def test_file_digest_does_not_block_on_a_fifo(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(IntegrityError):
        file_digest(tmp_path / "pipe")


def test_an_extra_file_alone_fails_verification(tree):
    m = build_manifest(tree)
    (tree / "surprise.txt").write_bytes(b"x")
    r = verify_manifest(tree, m)
    assert not r.ok and r.extra == ("surprise.txt",) and r.changed == r.missing == ()


def test_a_missing_file_alone_fails_verification(tree):
    m = build_manifest(tree)
    (tree / "a.txt").unlink()
    r = verify_manifest(tree, m)
    assert not r.ok and r.missing == ("a.txt",) and r.changed == r.extra == ()
