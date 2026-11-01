#!/usr/bin/env python3
"""Break proof: for each protection, break it in a temp COPY and require a test to fail.

The real source is never modified. Exit 0 only if the baseline is green and every
mutation is caught (tests fail) and every mutation anchor was found exactly once.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent

# (label, file under src/ragleap_trace, old text (must occur exactly once), new text)
MUTATIONS = [
    ("content keys no longer denied", "_span.py",
     "if _norm_key(key) in DENIED_KEYS:", "if False:"),
    ("key matching made case-sensitive", "_span.py",
     "return key.strip().lower().replace(\"-\", \"_\")", "return key"),
    ("string truncation removed", "_span.py",
     "value = _cut(value, MAX_STR_LEN)", "pass"),
    ("name truncation removed", "_span.py",
     "self.name = _cut(str(name), MAX_NAME_LEN)", "self.name = str(name)"),
    ("key truncation removed", "_span.py",
     "key = _cut(key, MAX_KEY_LEN)", "pass"),
    ("attribute count limit removed", "_span.py",
     "if key not in self.attributes and len(self.attributes) >= MAX_ATTRIBUTES:",
     "if False:"),
    ("dropped attributes not counted for limit", "_span.py",
     "        if key not in self.attributes and len(self.attributes) >= MAX_ATTRIBUTES:\n            self.dropped_attributes += 1\n",
     "        if key not in self.attributes and len(self.attributes) >= MAX_ATTRIBUTES:\n"),
    ("object values stored via repr()", "_span.py",
     "if not isinstance(value, (str, int, float, bool)):\n            self.dropped_attributes += 1\n            return False",
     "if not isinstance(value, (str, int, float, bool)):\n            value = repr(value)"),
    ("non-finite floats allowed", "_span.py",
     "if isinstance(value, float) and not math.isfinite(value):", "if False:"),
    ("empty/non-string keys allowed", "_span.py",
     "if not isinstance(key, str) or not key:", "if False:"),
    ("exception message recorded", "_span.py",
     "self.error_type = _cut(type(exc).__name__, MAX_NAME_LEN)",
     "self.error_type = _cut(type(exc).__name__ + ': ' + str(exc), 10000)"),
    ("error status not set on exception", "_span.py",
     "            self.status = \"error\"\n", "            pass\n"),
    ("exporter errors propagate to caller", "_tracer.py",
     "            except Exception:  # never propagate into the traced code\n                with self._lock:\n                    self.dropped += 1",
     "            except ZeroDivisionError:\n                pass"),
    ("exporter errors not counted", "_tracer.py",
     "                with self._lock:\n                    self.dropped += 1", "                pass"),
    ("caller exception swallowed (sync)", "_tracer.py",
     "        self._end(exc)\n        return False  # never swallow the caller's exception",
     "        self._end(exc)\n        return True"),
    ("caller exception swallowed (async)", "_tracer.py",
     "        self._end(exc)\n        return False\n\n\ndef traced",
     "        self._end(exc)\n        return True\n\n\ndef traced"),
    ("span not recorded when body raises", "_tracer.py",
     "    def __exit__(self, exc_type, exc, tb) -> bool:\n        self._end(exc)",
     "    def __exit__(self, exc_type, exc, tb) -> bool:\n        if exc is None:\n            self._end(exc)"),
    ("parent link removed", "_tracer.py",
     "parent.span_id if parent else None", "None"),
    ("trace id not shared with parent", "_tracer.py",
     "trace_id = parent.trace_id if parent else secrets.token_hex(16)",
     "trace_id = secrets.token_hex(16)"),
    ("current span never reset (siblings nest)", "_tracer.py",
     "                _current.reset(self._token)", "                pass"),
    ("JSONL file mode widened to 0644", "exporters.py",
     "os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600", "os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644"),
    ("JSONL accepts group/other-readable file", "exporters.py",
     "if mode & 0o077:", "if False:"),
    ("JSONL export after close not an error", "exporters.py",
     "                raise ValueError(\"exporter is closed\")", "                return"),
    ("JSONL not one object per line", "exporters.py",
     "+ \"\\n\"", "+ \" \""),
]


def run_tests(src: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(src / "src"), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
        cwd=src, env=env, capture_output=True, text=True,
    )


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(PKG, base, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "dist"))
        r = run_tests(base)
        if r.returncode != 0:
            print("BASELINE FAILED (tests must pass before mutating):")
            print(r.stdout[-2000:])
            return 2
        print("baseline: tests pass")
        for i, (label, fname, old, new) in enumerate(MUTATIONS):
            work = Path(tmp) / f"m{i}"
            shutil.copytree(base, work)
            target = work / "src" / "ragleap_trace" / fname
            text = target.read_text()
            n = text.count(old)
            if n != 1:
                print(f"ANCHOR ERROR [{label}]: anchor found {n} times in {fname}")
                failures += 1
                continue
            target.write_text(text.replace(old, new))
            r = run_tests(work)
            if r.returncode == 0:
                print(f"NOT CAUGHT  [{label}]  <- a protection without a failing test")
                failures += 1
            else:
                print(f"caught      [{label}]")
    total = len(MUTATIONS)
    print(f"\n{total - failures}/{total} mutations caught")
    if failures:
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
