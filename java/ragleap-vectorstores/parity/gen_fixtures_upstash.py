"""Recorded-request parity generator for UpstashBackend (ragleap-vectorstores).

Runs the REAL pinned Python backend with the real upstash-vector 0.8.0 SDK against a fake HTTP server
that records every request (method, raw path, JSON body, Authorization header) and answers from a scripted
queue; the SQLite document registry is real (a fresh temp file per scenario). The Java test replays the same
scripts against its own fake server and compares the requests and results. Not live-verified: no real Upstash.

Needs: git archive <pin> packages/ragleap-vectorstores | tar -x -C /root/py-vs-pinned
       venv with upstash-vector==0.8.0 (/root/venv-vs)
"""
import http.server
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
from urllib.parse import urlsplit

SRC = "/root/py-vs-pinned/packages/ragleap-vectorstores/src/ragleap_vectorstores/upstash_backend.py"
OUT = os.environ.get("RAGLEAP_VS_PARITY_DIR", "/root/fixtures-vs")
PIN = os.environ.get("VS_PIN", "unknown")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?$")

for _name in ("ragleap", "ragleap.vectorstores", "ragleap.vectorstores.base"):
    sys.modules[_name] = types.ModuleType(_name)


class VectorBackend:
    pass


sys.modules["ragleap.vectorstores.base"].VectorBackend = VectorBackend

spec = importlib.util.spec_from_file_location("upstash_backend_pinned", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.__file__ == SRC, "wrong module path"

import upstash_vector  # noqa: E402
from importlib.metadata import version as _ver  # noqa: E402
from upstash_vector.errors import UpstashError  # noqa: E402


class Fake:
    script = []
    log = []
    unscripted = 0


fake = Fake()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        fake.log.append({
            "method": self.command,
            "path": self.path.split("?", 1)[0],
            "query": self.path.split("?", 1)[1] if "?" in self.path else "",
            "body": json.loads(raw) if raw else None,
            "authorization": self.headers.get("Authorization"),
        })
        if fake.script:
            status, resp = fake.script.pop(0)
        else:
            fake.unscripted += 1
            status, resp = 418, {"error": "unscripted"}
        data = b"" if resp is None else json.dumps(resp).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    do_GET = do_PUT = do_DELETE = do_HEAD = do_POST


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = server.server_address[1]
BASE = "http://127.0.0.1:%d" % PORT
threading.Thread(target=server.serve_forever, daemon=True).start()


def J(status, body=None):
    return [status, body]


def info(dim=8, dense=True, sparse=False, top_dim=None):
    return {"result": {
        "vectorCount": 0, "pendingVectorCount": 0, "indexSize": 0,
        "dimension": dim if top_dim is None else top_dim, "similarityFunction": "COSINE",
        "denseIndex": {"dimension": dim, "similarityFunction": "COSINE", "embeddingModel": "x"} if dense else None,
        "sparseIndex": {"embeddingModel": "BM25"} if sparse else None,
        "namespaces": {"": {"vectorCount": 0, "pendingVectorCount": 0}},
    }}


OK_UPSERT = {"result": "Success"}
cases = []


def case(name, ops, script, ctor=None, lenient=False):
    cases.append({"name": name, "ctor": ctor or {}, "ops": ops, "script": script, "lenient": lenient})


def op(call, *args):
    return {"call": call, "args": list(args)}


def vec_hits(*items):
    return {"result": list(items)}


def hit(id_, score, meta):
    d = {"id": id_, "score": score}
    if meta is not None:
        d["metadata"] = meta
    return d


def rng(*ids):
    return {"result": {"nextCursor": "", "vectors": [{"id": i} for i in ids]}}


INIT = [J(200, info())]

case("init_ok_dense", [op("init_schema", 8)], INIT)
case("init_hybrid_index", [op("init_schema", 8)], [J(200, info(sparse=True))])
case("init_no_dense_index", [op("init_schema", 8)], [J(200, info(dense=False))])
case("init_dimension_mismatch", [op("init_schema", 8)], [J(200, info(dim=4))])
case("init_uses_top_level_dimension", [op("init_schema", 8)], [J(200, info(dim=4, top_dim=8))])
case("init_remote_error", [op("init_schema", 8)], [J(200, {"error": "Unauthorized"})])
case("init_error_status_with_error_body", [op("init_schema", 8)], [J(500, {"error": "boom"})])
case("init_empty_body_500", [op("init_schema", 8)], [J(500, None)])
case("init_body_without_result", [op("init_schema", 8)], [J(200, {})])
case("registry_before_init", [
    op("insert_document", "doc1", "a.txt", {"k": "v", "n": 1, "u": "\u00e9", "nested": {"a": [1, None, True]}}),
    op("insert_document", "doc:2", "b \u00e9.txt", None),
    op("insert_document", "d3", "c.txt", {}),
    op("insert_document", "", "empty-id.txt", {"x": 1.5}),
    op("list_documents", 10, 0),
    op("list_documents", 2, 1),
    op("list_documents", 0, 0),
    op("get_document_filename", "doc:2"),
    op("get_document_filename", "nope"),
    op("supports_sparse"),
], [])
case("insert_document_duplicate", [
    op("insert_document", "doc1", "a.txt", {}),
    op("insert_document", "doc1", "other.txt", {}),
    op("get_document_filename", "doc1"),
], [])
case("chunk_and_search_before_init", [
    op("insert_chunk", "doc1", "a.txt", 0, "t", 1, [0.1], {}),
    op("search_dense", [0.1, 0.2], 3, None),
    op("delete_document", "doc1"),
], [])
case("insert_chunk_variants", [
    op("init_schema", 8),
    op("insert_chunk", "doc1", "a.txt", 0, "hello", 5, [0.1, 0.2, 0.3], {"page": 1}),
    op("insert_chunk", "doc1", "a.txt", 1, "h\u00e9llo \"q\"\n", None, [1.0, -0.5, 1e-07], None),
    op("insert_chunk", "doc:x", "n", 2, "keep", 3, [0.0], {"text": "OVERRIDE", "document_id": "evil", "extra": {"a": [1, 2]}}),
    op("insert_chunk", "", "n", 0, "", 0, [], {}),
], INIT + [J(200, OK_UPSERT)] * 4)
case("insert_chunk_error", [op("init_schema", 8), op("insert_chunk", "d", "n", 0, "t", 1, [0.5], {})],
     INIT + [J(200, {"error": "bad vector"})])
HITS = [
    hit("doc1:0", 0.912345, {"text": "first \u00e9", "document_id": "doc1", "document_name": "a.txt", "chunk_index": 0, "token_count": 5}),
    hit("doc1:1", 0.12345, {"text": "second", "document_id": "doc1", "document_name": "a.txt", "chunk_index": 1}),
    hit("doc2:0", 0.99995, None),
    hit("doc3:0", 0.00005, {}),
    hit("doc4:0", 1, {"text": "", "document_id": None, "document_name": None, "chunk_index": 2}),
    hit("doc5:0", 0, {"text": "z", "document_id": "doc5", "chunk_index": "7"}),
    hit("doc6:0", 0.55555, {"text": "y", "document_id": "doc6", "document_name": "f", "chunk_index": 3.0}),
    hit("doc7:0", 0.88885, {"text": "x", "document_id": "doc7", "document_name": "g", "chunk_index": 4}),
]
case("search_plain", [op("init_schema", 8), op("search_dense", [0.1, 0.2], 3, None)], INIT + [J(200, vec_hits(*HITS))])
case("search_filters", [
    op("init_schema", 8),
    op("search_dense", [1.0], 2, {"document_id": "it's", "flag": True, "n": 3, "f": 1.5, "g": 1e-07, "none": None, "sp": "x y", "off": False}),
    op("search_dense", [1.0], 2, {}),
    op("search_dense", [], 2, {"a": 1}),
], INIT + [J(200, vec_hits()), J(200, vec_hits())])
case("search_filter_key_validation", [
    op("init_schema", 8),
    op("search_dense", [1.0], 2, {"a.b_c": 1, "_x9": "y"}),
    op("search_dense", [1.0], 2, {"bad key": 1}),
    op("search_dense", [1.0], 2, {"1abc": 1}),
    op("search_dense", [1.0], 2, {"ok": 1, "x;drop": 2}),
    op("search_dense", [1.0], 2, {"": 1}),
    op("search_dense", [1.0], 2, {"a-b": 1}),
    op("search_dense", [1.0], 2, {"cl\u00e9": 1}),
    op("search_dense", [1.0], 2, {"it's": 1}),
    op("search_dense", [], 2, {"bad key": 1}),
], INIT + [J(200, vec_hits())])
case("search_error", [op("init_schema", 8), op("search_dense", [0.5], 2, None)], INIT + [J(200, {"error": "bad filter"})])
case("search_result_missing_id", [op("init_schema", 8), op("search_dense", [0.5], 2, None)],
     INIT + [J(200, vec_hits({"score": 0.5}))])
case("namespace_paths", [
    op("init_schema", 8),
    op("insert_document", "d1", "f.txt", {}),
    op("insert_chunk", "d1", "f.txt", 0, "t", 1, [0.5], {}),
    op("search_dense", [0.5], 2, None),
    op("list_documents", 5, 0),
    op("delete_document", "d1"),
], INIT + [J(200, OK_UPSERT), J(200, vec_hits()), J(200, rng("d1:0", "d1:1")), J(200, {"result": {"deleted": 2}})],
     ctor={"namespace": "ns1"})
case("namespace_special_chars", [
    op("init_schema", 8),
    op("insert_chunk", "d1", "f.txt", 0, "t", 1, [0.5], {}),
], INIT + [J(200, OK_UPSERT)], ctor={"namespace": "my ns/\u00e9"})
case("list_with_index", [
    op("init_schema", 8),
    op("insert_document", "d1", "a.txt", {"a": 1}),
    op("insert_document", "d:2", "b.txt", {}),
    op("list_documents", 10, 0),
    op("list_documents", 1, 1),
], INIT + [J(200, rng("d:2:0", "d:2:1")), J(200, rng("d1:0", "d1:1", "d1:2")), J(200, rng("d1:0"))])
case("list_range_error", [
    op("init_schema", 8), op("insert_document", "d1", "a.txt", {}), op("list_documents", 5, 0),
], INIT + [J(200, {"error": "x"})])
case("delete_sequence", [
    op("init_schema", 8),
    op("insert_document", "d1", "a.txt", {}),
    op("delete_document", "d1"),
    op("get_document_filename", "d1"),
    op("delete_document", "d1"),
    op("delete_document", "ghost"),
    op("insert_document", "d9", "z.txt", {}),
    op("delete_document", "d9"),
    op("get_document_filename", "d9"),
], INIT + [J(200, {"result": {"deleted": 1}}), J(200, {"result": {"deleted": 0}}), J(200, {"result": {"deleted": 0}}),
           J(200, {"error": "delete failed"})])

case("init_status_500_with_result_body", [op("init_schema", 8)], [J(500, info())])
case("init_dense_empty_object", [op("init_schema", 8)], [J(200, {"result": {**info()["result"], "denseIndex": {}}})])
case("init_sparse_empty_object_is_ignored", [op("init_schema", 8)], [J(200, {"result": {**info()["result"], "sparseIndex": {}}})])
case("init_failed_then_use", [
    op("init_schema", 8),
    op("insert_chunk", "d", "n", 0, "t", 1, [0.5], {}),
    op("search_dense", [0.5], 2, None),
], [J(200, {"error": "x"}), J(200, OK_UPSERT), J(200, vec_hits())])
case("list_range_missing_cursor", [
    op("init_schema", 8), op("insert_document", "d1", "a.txt", {}), op("list_documents", 5, 0),
], INIT + [J(200, {"result": {"vectors": []}})])
case("list_range_vector_without_id", [
    op("init_schema", 8), op("insert_document", "d1", "a.txt", {}), op("list_documents", 5, 0),
], INIT + [J(200, {"result": {"nextCursor": "", "vectors": [{"metadata": {}}]}})])
case("namespace_path_chars_a", [
    op("init_schema", 8), op("insert_chunk", "d1", "f.txt", 0, "t", 1, [0.5], {}),
], INIT + [J(200, OK_UPSERT)], ctor={"namespace": "a+b,c:d@e!f$g&h'i(j)k*l;m=n~o_p-q.r"})
case("namespace_path_chars_b", [
    op("init_schema", 8), op("insert_chunk", "d1", "f.txt", 0, "t", 1, [0.5], {}),
], INIT + [J(200, OK_UPSERT)], ctor={"namespace": "%41 %zz [x] {y} \"z\" <w> |^`"}, lenient=True)

case("namespace_path_chars_c", [
    op("init_schema", 8), op("insert_chunk", "d1", "f.txt", 0, "t", 1, [0.5], {}),
], INIT + [J(200, OK_UPSERT)], ctor={"namespace": "back\\slash\ttab\x7fdel \u00e9 \U0001F600 {}"}, lenient=True)
case("trailing_slash_url", [
    op("init_schema", 8), op("insert_chunk", "d1", "f.txt", 0, "t", 1, [0.5], {}),
], INIT + [J(200, OK_UPSERT)], ctor={"url_suffix": "/", "raw": True})


def rngc(cursor, *ids):
    return {"result": {"nextCursor": cursor, "vectors": [{"id": i} for i in ids]}}


def paging_case(name, pages, ctor=None, docs=("d1",)):
    ops = [op("init_schema", 8)] + [op("insert_document", d, d + ".txt", {}) for d in docs] + [op("list_documents", 10, 0)]
    case(name, ops, INIT + pages, ctor=ctor)


paging_case("list_paging_three_pages", [
    J(200, rngc("c1", "d2:0", "d2:1")), J(200, rngc("c2", "d2:2")), J(200, rngc("", "d2:3", "d2:4")),
    J(200, rngc("", "d1:0"))], docs=("d1", "d2"))
paging_case("list_paging_repeated_cursor", [J(200, rngc("c1", "d1:0", "d1:1")), J(200, rngc("c1", "d1:2"))])
paging_case("list_paging_cursor_zero_string", [J(200, rngc("0", "d1:0")), J(200, rngc("", "d1:1"))])
paging_case("list_paging_empty_page_then_data", [J(200, rngc("c1")), J(200, rngc("", "d1:0"))])
paging_case("list_paging_second_page_error", [J(200, rngc("c1", "d1:0")), J(200, {"error": "boom"})])
paging_case("list_paging_second_page_missing_vectors", [J(200, rngc("c1", "d1:0")), J(200, {"result": {"nextCursor": ""}})])
paging_case("list_paging_null_cursor", [J(200, {"result": {"nextCursor": None, "vectors": [{"id": "d1:0"}]}})])
paging_case("list_paging_namespace", [J(200, rngc("c1", "d1:0")), J(200, rngc("", "d1:1"))], ctor={"namespace": "ns1"})


case("insert_chunk_metadata_collisions", [
    op("init_schema", 8),
    op("insert_chunk", "doc1", "a.txt", 3, "real", 7, [0.1, 0.2],
       {"text": "EVIL", "document_id": "evil", "document_name": "x", "chunk_index": 99, "token_count": 0, "page": 2}),
    op("insert_chunk", "d", "n", 1, "t", None, [0.5], {"token_count": 5, "lang": "en"}),
], INIT + [J(200, OK_UPSERT)] * 2)


def classify(e):
    if isinstance(e, (UpstashError, KeyError, json.JSONDecodeError)):
        return {"error": "RemoteError"}
    if isinstance(e, sqlite3.IntegrityError):
        return {"error": "IntegrityError"}
    if isinstance(e, RuntimeError):
        return {"error": "RuntimeError", "message": str(e).replace(BASE, "<url>")}
    if isinstance(e, AttributeError):
        return {"error": "NotInitialized"}
    return {"error": type(e).__name__, "message": str(e)[:120]}


def run_ops(be, ops):
    out = []
    for o in ops:
        try:
            res = getattr(be, o["call"])(*o["args"])
            if o["call"] == "list_documents":
                for d in res:
                    assert ISO.match(d["uploaded_at"]), d["uploaded_at"]
                    d["uploaded_at"] = "<iso-naive>"
            out.append({"ok": res})
        except Exception as e:  # noqa: BLE001
            out.append(classify(e))
    return out


import httpx  # noqa: E402


def install_mock():
    """Route the SDK through an httpx mock transport: records the exact path the SDK sends (the fake server
    above would collapse a leading double slash) and answers from the same scripted queue."""
    orig = upstash_vector.Index

    class MockIndex(orig):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)

            def handler(request):
                body = request.content
                fake.log.append({
                    "method": request.method,
                    "path": request.url.raw_path.decode().split("?", 1)[0],
                    "query": "",
                    "body": json.loads(body) if body else None,
                    "authorization": request.headers.get("authorization"),
                })
                if fake.script:
                    status, resp = fake.script.pop(0)
                else:
                    fake.unscripted += 1
                    status, resp = 418, {"error": "unscripted"}
                return httpx.Response(status, json=resp) if resp is not None else httpx.Response(status)

            self._client = httpx.Client(transport=httpx.MockTransport(handler))

    upstash_vector.Index = MockIndex
    return orig


tmp = tempfile.mkdtemp(prefix="vs-upstash-")
for i, c in enumerate(cases):
    fake.script = [list(x) for x in c["script"]]
    fake.log = []
    fake.unscripted = 0
    be = mod.UpstashBackend(BASE + c["ctor"].get("url_suffix", ""), "tok", namespace=c["ctor"].get("namespace", ""),
                            registry_path=os.path.join(tmp, "reg%d.sqlite3" % i))
    _orig = install_mock() if c["ctor"].get("raw") else None
    c["results"] = run_ops(be, c["ops"])
    if _orig is not None:
        upstash_vector.Index = _orig
    c["log"] = fake.log
    be._conn.close()
    assert fake.unscripted == 0, (c["name"], "unscripted request")
    assert c["lenient"] or not fake.script, (c["name"], "scripted answers left over", len(fake.script))
    for r in c["log"]:
        assert r["authorization"] == "Bearer tok", (c["name"], r["authorization"])
        r.pop("authorization")
        r.pop("query")
assert cases[0]["results"] == [{"ok": None}], ("init_ok_dense must succeed", cases[0]["results"])
shutil.rmtree(tmp, ignore_errors=True)

ctor_cases = []
for url, token in [("", "t"), ("u", ""), (None, "t"), ("u", None), ("", "")]:
    try:
        mod.UpstashBackend(url, token, registry_path=os.path.join(tempfile.gettempdir(), "x-ctor.sqlite3"))
        res = {"ok": True}
    except Exception as e:  # noqa: BLE001
        res = {"error": type(e).__name__, "message": str(e)}
    ctor_cases.append({"url": url, "token": token, "result": res})

os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "upstash_cases.json"), "w", encoding="utf-8") as f:
    json.dump({"pin": PIN, "python": sys.version.split()[0], "upstash_vector": _ver("upstash-vector"),
               "cases": cases, "ctor_cases": ctor_cases}, f)

total = 0
for c in cases:
    total += len(c["log"])
    print("##", c["name"], "| requests:", len(c["log"]), "|", json.dumps(c["results"])[:120])
    for r in c["log"]:
        print("   ", r["method"], r["path"], json.dumps(r["body"])[:118] if r["body"] is not None else "")
print("cases:", len(cases), "requests:", total, "ctor_cases:", len(ctor_cases))
print("upstash-vector", _ver("upstash-vector"), "python", sys.version.split()[0])
