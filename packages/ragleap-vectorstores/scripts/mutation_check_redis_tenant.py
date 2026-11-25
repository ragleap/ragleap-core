"""Deliberate-break check for the Redis tenant-case protections (0.7.1).

Each mutation breaks one protection in a temporary copy of the package; at least one test in
tests/test_tenant_filters.py must fail. Run from packages/ragleap-vectorstores:
    python scripts/mutation_check_redis_tenant.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL = "src/ragleap_vectorstores/redis_backend.py"

MUTATIONS = [
    ("tenant field not case sensitive", 'TagField("tenant_id", case_sensitive=True)', 'TagField("tenant_id")'),
    ("document field not case sensitive", 'TagField("document_id", case_sensitive=True)', 'TagField("document_id")'),
    ("search: no exact check", "if any(_s(getattr(doc, k, None)) != want for k, want in exact.items()):",
     "if False:"),
    ("search: check inverted", "if any(_s(getattr(doc, k, None)) != want for k, want in exact.items()):",
     "if any(_s(getattr(doc, k, None)) == want for k, want in exact.items()):"),
    ("search: all instead of any", "if any(_s(getattr(doc, k, None)) != want for k, want in exact.items()):",
     "if all(_s(getattr(doc, k, None)) != want for k, want in exact.items()):"),
    ("search: tenant_id not checked", 'if k in ("document_id", "tenant_id")}\n        results = []',
     'if k in ("document_id",)}\n        results = []'),
    ("search: document_id not checked", 'if k in ("document_id", "tenant_id")}\n        results = []',
     'if k in ("tenant_id",)}\n        results = []'),
    ("search: tenant_id field not requested",
     '"document_name", "chunk_index", "tenant_id")\n            .paging(0, top_k)',
     '"document_name", "chunk_index")\n            .paging(0, top_k)'),
    ("search: bytes not decoded", "return value.decode(\"utf-8\", \"replace\") if isinstance(value, bytes) else value",
     "return value"),
    ("delete: no exact check", 'for doc in res.docs if _s(getattr(doc, "document_id", None)) == str(document_id)]',
     "for doc in res.docs]"),
    ("delete: document_id not requested", '.return_fields("document_id").paging(0, 10000)', ".paging(0, 10000)"),
    ("info: always True", 'return "CASESENSITIVE" in (w.upper() for w in words)', "return True"),
    ("info: always False", 'return "CASESENSITIVE" in (w.upper() for w in words)', "return False"),
    ("info: field name ignored", 'if name in words and "TAG" in (w.upper() for w in words):',
     'if "TAG" in (w.upper() for w in words):'),
    ("info: type ignored", 'if name in words and "TAG" in (w.upper() for w in words):', "if name in words:"),
    ("info: bytes keys ignored", 'attrs = info.get("attributes", info.get(b"attributes"))',
     'attrs = info.get("attributes")'),
    ("info: unknown treated as fine", 'is not True:\n                logger.warning(', 'is False:\n                logger.warning('),
    ("warning removed", "without regard to case (it was", "XXXX (it was"),
]


def main():
    killed = 0
    survivors = []
    for name, old, new in MUTATIONS:
        tmp = tempfile.mkdtemp()
        try:
            work = os.path.join(tmp, "pkg")
            shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", ".pytest_cache"))
            path = os.path.join(work, REL)
            text = open(path, encoding="utf-8").read()
            if text.count(old) != 1:
                print(f"ANCHOR_COUNT_{text.count(old)}: {name}")
                sys.exit(2)
            open(path, "w", encoding="utf-8").write(text.replace(old, new))
            env = dict(os.environ, PYTHONPATH=os.path.join(work, "src"))
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/test_tenant_filters.py"],
                cwd=work, env=env, capture_output=True, text=True,
            )
            if r.returncode != 0:
                killed += 1
                print("KILLED  ", name)
            else:
                survivors.append(name)
                print("SURVIVED", name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"MUTATIONS {killed} of {len(MUTATIONS)} killed")
    sys.exit(0 if not survivors else 1)


if __name__ == "__main__":
    main()
