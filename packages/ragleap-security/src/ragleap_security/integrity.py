"""File integrity: bounded SHA-256 digests and a manifest of a directory. Standard library only.

This DETECTS changes. It does not prevent copying, and an unsigned manifest can be rewritten together
with the files. With a Key the manifest carries an HMAC, which only holders of the key can produce."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Tuple, Union

from .keys import Key
from .limits import LimitError, check_json_shape, check_size

FORMAT = "ragleap-security-manifest"
VERSION = 1
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class IntegrityError(Exception):
    """A file or manifest cannot be read or is invalid, or a limit was reached."""


def _open_regular(path: Union[str, "os.PathLike[str]"]) -> int:
    # O_NONBLOCK: opening a FIFO for reading would otherwise wait forever for a writer.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise IntegrityError("cannot open file (missing, unreadable or a symlink)") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise IntegrityError("not a regular file")
    return fd


def file_digest(path: Union[str, "os.PathLike[str]"], *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> str:
    """SHA-256 (hex) of a regular file, read in chunks. Symlinks and non-regular files are refused.
    Raises LimitError if the file is larger than max_bytes."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative int")
    h = hashlib.sha256()
    total = 0
    with os.fdopen(_open_regular(path), "rb") as fp:
        while True:
            chunk = fp.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise LimitError(f"file is larger than {max_bytes} bytes")
            h.update(chunk)
    return h.hexdigest()


def _check_rel(path: object) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or path.startswith("/"):
        raise IntegrityError("invalid path in manifest")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise IntegrityError("invalid path in manifest")
    return path


@dataclass(frozen=True)
class Manifest:
    """Relative path (always with "/") to SHA-256 hex, and an optional HMAC over them."""

    files: Mapping[str, str]
    mac: Optional[str] = None

    def to_json(self) -> str:
        """Canonical JSON (sorted keys, no extra whitespace)."""
        return json.dumps({"format": FORMAT, "version": VERSION, "files": dict(self.files), "mac": self.mac},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @classmethod
    def from_json(cls, text: Union[str, bytes]) -> "Manifest":
        """Parse and strictly validate a manifest (size, shape, paths, digests)."""
        check_size(text, MAX_MANIFEST_BYTES, "manifest")
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            raise IntegrityError("manifest is not valid JSON") from None
        check_json_shape(data, max_depth=4, max_nodes=2 * DEFAULT_MAX_FILES + 100)
        if not isinstance(data, dict) or set(data) != {"format", "version", "files", "mac"}:
            raise IntegrityError("manifest has the wrong fields")
        if data["format"] != FORMAT or data["version"] != VERSION or isinstance(data["version"], bool):
            raise IntegrityError("unsupported manifest format or version")
        files = data["files"]
        if not isinstance(files, dict):
            raise IntegrityError("manifest files must be an object")
        if len(files) > DEFAULT_MAX_FILES:
            raise IntegrityError("manifest lists too many files")
        for path, digest in files.items():
            _check_rel(path)
            if not isinstance(digest, str) or not _HEX64.match(digest):
                raise IntegrityError("invalid digest in manifest")
        mac = data["mac"]
        if mac is not None and (not isinstance(mac, str) or not _HEX64.match(mac)):
            raise IntegrityError("invalid mac in manifest")
        return cls(files=files, mac=mac)


def _canonical(files: Mapping[str, str]) -> bytes:
    """The exact bytes the HMAC covers: format, version and every path with its digest."""
    return json.dumps({"format": FORMAT, "version": VERSION, "files": dict(files)},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _mac(files: Mapping[str, str], key: Key) -> str:
    if type(key) is not Key:
        raise TypeError("key must be a Key")
    return hmac.new(key.derive(b"manifest-mac"), _canonical(files), hashlib.sha256).hexdigest()


def _scan(root: Union[str, "os.PathLike[str]"], exclude: Iterable[str], max_files: int) -> Tuple[Dict[str, str], list]:
    """Return ({relative path: full path} of regular files, [relative paths of symlinks and other
    special entries]). Symlinks are never followed."""
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
        raise ValueError("max_files must be a positive int")
    skip = frozenset(exclude)
    root = os.fspath(root)
    if not os.path.isdir(root):
        raise IntegrityError("root is not a directory")

    def fail(_err: OSError) -> None:
        raise IntegrityError("cannot read a directory")

    regular: Dict[str, str] = {}
    special: list = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=fail):
        keep = []
        for name in sorted(dirnames):
            if name in skip:
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                special.append(os.path.relpath(full, root).replace(os.sep, "/"))
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if name in skip:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full) or not stat.S_ISREG(os.lstat(full).st_mode):
                special.append(rel)
            else:
                regular[rel] = full
        if len(regular) + len(special) > max_files:
            raise IntegrityError("too many files")
    return regular, special


def build_manifest(root: Union[str, "os.PathLike[str]"], *, exclude: Iterable[str] = (), key: Optional[Key] = None,
                   max_files: int = DEFAULT_MAX_FILES, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> Manifest:
    """Digest every regular file under `root`. `exclude` is a set of file or directory NAMES to skip
    (pass the same set to verify_manifest). Symlinks and special files make this fail, because their
    content would not be covered. With `key` the manifest gets an HMAC."""
    regular, special = _scan(root, exclude, max_files)
    if special:
        raise IntegrityError("refusing symlinks and special files: " + ", ".join(sorted(special)[:5]))
    files = {rel: file_digest(full, max_bytes=max_file_bytes) for rel, full in sorted(regular.items())}
    manifest = Manifest(files=files)
    if key is not None:
        manifest = Manifest(files=files, mac=_mac(files, key))
    return manifest


@dataclass(frozen=True)
class IntegrityResult:
    ok: bool
    changed: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    extra: Tuple[str, ...] = ()
    mac_checked: bool = False
    error: Optional[str] = None


def verify_manifest(root: Union[str, "os.PathLike[str]"], manifest: Manifest, *, key: Optional[Key] = None,
                    exclude: Iterable[str] = (), max_files: int = DEFAULT_MAX_FILES,
                    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> IntegrityResult:
    """Compare `root` with a manifest. With `key`, the manifest must carry a valid HMAC (an unsigned
    manifest is refused, so a signature cannot be stripped). Files listed in the manifest but not found
    are `missing`; files found but not listed are `extra`; a listed file whose digest differs, that
    cannot be read, or that is now a symlink is `changed`. Never follows symlinks and never opens a
    path taken from the manifest."""
    if type(manifest) is not Manifest:
        raise TypeError("manifest must be a Manifest")
    if key is not None and type(key) is not Key:
        raise TypeError("key must be a Key")
    mac_checked = False
    if key is not None:
        if manifest.mac is None:
            return IntegrityResult(False, error="manifest is not signed")
        if not hmac.compare_digest(manifest.mac, _mac(manifest.files, key)):
            return IntegrityResult(False, mac_checked=True, error="manifest signature does not match")
        mac_checked = True
    try:
        regular, special = _scan(root, exclude, max_files)
    except IntegrityError as exc:
        return IntegrityResult(False, mac_checked=mac_checked, error=str(exc))
    listed = manifest.files
    changed, extra = [], []
    for rel in special:
        (changed if rel in listed else extra).append(rel)
    for rel, full in regular.items():
        if rel not in listed:
            extra.append(rel)
            continue
        try:
            same = hmac.compare_digest(file_digest(full, max_bytes=max_file_bytes), listed[rel])
        except (IntegrityError, LimitError, OSError):
            same = False
        if not same:
            changed.append(rel)
    missing = [rel for rel in listed if rel not in regular and rel not in special]
    ok = not (changed or missing or extra)
    return IntegrityResult(ok, tuple(sorted(changed)), tuple(sorted(missing)), tuple(sorted(extra)), mac_checked)
