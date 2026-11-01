#!/usr/bin/env python3
"""Break proof for the integration tests, run against TEMP COPIES of ragleap-trace and ragleap-rag.

Each mutation breaks one thing (in the adapter, the tracer, or ragleap-rag itself, to simulate
drift) and the integration tests must FAIL (pytest exit code 1; an import or collection error
does not count). The real sources are never modified. Needs the same Postgres as the tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TRACE = Path(__file__).resolve().parent.parent
RAG = TRACE.parent / "ragleap-rag"
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "dist", "*.egg-info", "tests", ".venv")

# (label, which package, file relative to that package's src/<module>/, old (exactly once), new)
MUTATIONS = [
    ("adapter ignores answer events", "trace", "ragleap_trace/rag.py",
     "    def on_answer(self, event: Any) -> None:\n        self._safe(self._answer, event)",
     "    def on_answer(self, event: Any) -> None:\n        return"),
    ("adapter hooks wired to the wrong events", "trace", "ragleap_trace/rag.py",
     '"on_query": [self.on_query],\n            "on_answer": [self.on_answer],',
     '"on_query": [self.on_answer],\n            "on_answer": [self.on_query],'),
    ("query text leaks under an innocent key", "trace", "ragleap_trace/rag.py",
     '        self.tracer.record("rag.query", **attrs)\n',
     '        attrs["q"] = event.get("query")\n        self.tracer.record("rag.query", **attrs)\n'),
    ("file name leaks under an innocent key", "trace", "ragleap_trace/rag.py",
     '        self.tracer.record("rag.ingest", **attrs)\n',
     '        attrs["f"] = event.get("filename")\n        self.tracer.record("rag.ingest", **attrs)\n'),
    ("query start time never recorded", "trace", "ragleap_trace/rag.py",
     "        self._pending.set(time.perf_counter())\n", "        pass\n"),
    ("record() loses the parent span", "trace", "ragleap_trace/_tracer.py",
     "            parent.span_id if parent else None,\n", "            None,\n"),
    ("exporter failures are not counted", "trace", "ragleap_trace/_tracer.py",
     "                with self._lock:\n                    self.dropped += 1",
     "                pass"),
    ("ragleap-rag renames chunks_sent", "rag", "ragleap/__init__.py",
     '"chunks_sent": result.get("chunks_sent")', '"chunks_sent_x": result.get("chunks_sent")'),
    ("ragleap-rag renames answer_length", "rag", "ragleap/__init__.py",
     '"answer_length": len(full_answer)', '"answer_len": len(full_answer)'),
    ("ragleap-rag renames chunks_stored", "rag", "ragleap/__init__.py",
     '"chunks_stored": stored, "chunks_attempted"', '"chunks_done": stored, "chunks_attempted"'),
    ("ragleap-rag stream query event says streaming False", "rag", "ragleap/__init__.py",
     '"chunks_retrieved": len(chunks), "streaming": True}', '"chunks_retrieved": len(chunks), "streaming": False}'),
    ("ragleap-rag fires on_answer for a stream stopped early", "rag", "ragleap/__init__.py",
     "            pieces.append(piece)\n            yield piece\n",
     "            pieces.append(piece)\n            try:\n                yield piece\n            except GeneratorExit:\n"
     '                fire_event({"query": query, "answer_length": len("".join(pieces)), "streaming": True},\n'
     '                           self._on_answer, "on_answer")\n                raise\n'),
]


def run(trace_dir: Path, rag_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join([str(trace_dir / "src"), str(rag_dir / "src")]))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "integration"],
        cwd=trace_dir, env=env, capture_output=True, text=True)


def main() -> int:
    if not (RAG / "src" / "ragleap" / "__init__.py").exists():
        print(f"STOP: ragleap-rag not found at {RAG}")
        return 2
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        trace_dir, rag_dir = Path(tmp) / "trace", Path(tmp) / "rag"
        shutil.copytree(TRACE, trace_dir, ignore=IGNORE)
        shutil.copytree(RAG, rag_dir, ignore=IGNORE)
        # the copies must be the ones imported, not the installed packages
        probe = subprocess.run(
            [sys.executable, "-c", "import ragleap, ragleap_trace; print(ragleap.__file__); print(ragleap_trace.__file__)"],
            cwd=trace_dir, capture_output=True, text=True,
            env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(trace_dir / "src"), str(rag_dir / "src")])))
        if probe.returncode != 0 or any(tmp not in line for line in probe.stdout.split()):
            print("STOP: temp copies are not the imported packages:\n" + probe.stdout + probe.stderr)
            return 2
        r = run(trace_dir, rag_dir)
        if r.returncode != 0:
            print("BASELINE FAILED:\n" + r.stdout[-1500:])
            return 2
        print("baseline: integration tests pass")
        for label, pkg, rel, old, new in MUTATIONS:
            target = (trace_dir if pkg == "trace" else rag_dir) / "src" / rel
            original = target.read_text()
            if original.count(old) != 1:
                print(f"ANCHOR ERROR [{label}]: found {original.count(old)} times in {rel}")
                bad += 1
                continue
            target.write_text(original.replace(old, new))
            try:
                r = run(trace_dir, rag_dir)
            finally:
                target.write_text(original)
            if r.returncode == 1:
                print(f"caught      [{label}]")
            elif r.returncode == 0:
                print(f"NOT CAUGHT  [{label}]")
                bad += 1
            else:
                print(f"ERROR rc={r.returncode} (not a test failure) [{label}]")
                bad += 1
    total = len(MUTATIONS)
    print(f"\n{total - bad}/{total} integration mutations caught")
    print("VERDICT: PASS" if not bad else "VERDICT: FAIL")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
