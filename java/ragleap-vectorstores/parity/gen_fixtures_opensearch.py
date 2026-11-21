"""Recorded-request parity generator for OpenSearchBackend (ragleap-vectorstores).

Runs the REAL pinned Python backend with the real opensearch-py client against a
fake HTTP server that records every request (method, raw path, query, JSON body,
Authorization header) and answers from a scripted queue. The Java test replays the
same scripts against its own fake server and compares the recorded requests and the
results. Not live-verified: no real OpenSearch is involved.

Needs: git archive <pin> packages/ragleap-vectorstores | tar -x -C /root/py-vs-pinned
       venv with opensearch-py==3.2.0 (/root/venv-vs)
"""
import http.server
import importlib.util
import json
import os
import re
import sys
import threading
import types
from urllib.parse import parse_qsl, urlsplit

SRC = "/root/py-vs-pinned/packages/ragleap-vectorstores/src/ragleap_vectorstores/opensearch_backend.py"
OUT = os.environ.get("RAGLEAP_VS_PARITY_DIR", "/root/fixtures-vs")
PIN = os.environ.get("VS_PIN", "unknown")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?\+00:00$")

# The abstract base class lives in ragleap-rag; importing the ragleap package pulls in the whole
# rag stack, so only the marker class is stubbed. The backend under test is the real file.
for _name in ("ragleap", "ragleap.vectorstores", "ragleap.vectorstores.base"):
    sys.modules[_name] = types.ModuleType(_name)


class VectorBackend:
    pass


sys.modules["ragleap.vectorstores.base"].VectorBackend = VectorBackend

spec = importlib.util.spec_from_file_location("opensearch_backend_pinned", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.__file__ == SRC, "wrong module path"

import opensearchpy  # noqa: E402
from opensearchpy.exceptions import TransportError  # noqa: E402


class Fake:
    script = []
    log = []
    unscripted = 0


fake = Fake()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        body = json.loads(raw) if raw else None
        if isinstance(body, dict) and isinstance(body.get("uploaded_at"), str) and ISO.match(body["uploaded_at"]):
            body["uploaded_at"] = "<iso-utc-micro>"
        u = urlsplit(self.path)
        fake.log.append({
            "method": self.command,
            "path": u.path,
            "query": sorted([list(p) for p in parse_qsl(u.query, keep_blank_values=True)]),
            "body": body,
            "authorization": self.headers.get("Authorization"),
        })
        if fake.script:
            status, resp = fake.script.pop(0)
        else:
            fake.unscripted += 1
            status, resp = 418, {"error": "unscripted"}
        data = b"" if (resp is None or self.command == "HEAD") else json.dumps(resp).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()


def J(status, body=None):
    return [status, body]


ACK = {"acknowledged": True}
CREATED = {"result": "created"}
cases = []


def case(name, ops, script, ctor=None):
    cases.append({"name": name, "ctor": ctor or {}, "ops": ops, "script": script})


def op(call, *args):
    return {"call": call, "args": list(args)}


def mapping(idx, dim):
    return {idx: {"mappings": {"properties": {"embedding": {"type": "knn_vector", "dimension": dim}}}}}


case("init_fresh", [op("init_schema", 8)], [J(404), J(200, ACK), J(404), J(200, ACK)])
case("init_existing_same_dim", [op("init_schema", 8)], [J(200), J(200, mapping("ragleap_idx", 8)), J(200, ACK), J(200)])
case("init_existing_put_mapping_fails", [op("init_schema", 8)],
     [J(200), J(200, mapping("ragleap_idx", 8)), J(500, {"error": "x"}), J(200)])
case("init_existing_dim_mismatch", [op("init_schema", 8)], [J(200), J(200, mapping("ragleap_idx", 4))])
case("init_existing_no_dim", [op("init_schema", 8)], [J(200), J(200, {}), J(200, ACK), J(404), J(200, ACK)])
case("init_custom_names", [op("init_schema", 3)], [J(404), J(200, ACK), J(404), J(200, ACK)],
     ctor={"index_name": "my_idx", "documents_index_name": "my_docs"})
case("init_default_docs_name_with_space", [op("init_schema", 3)], [J(404), J(200, ACK), J(404), J(200, ACK)],
     ctor={"index_name": "Weird idx"})
case("insert_document_variants", [
    op("insert_document", "doc1", "a.txt", {"k": "v", "n": 1, "f": 1.5, "u": "\u00e9", "nested": {"a": [1, 2, None, True]}}),
    op("insert_document", "doc:2", "b \u00e9.txt", None),
    op("insert_document", "d3", "c.txt", {}),
], [J(200, CREATED), J(200, CREATED), J(200, CREATED)])
case("insert_chunk_variants", [
    op("insert_chunk", "doc1", "a.txt", 0, "hello", 5, [0.1, 0.2, 0.3], {"page": 1}),
    op("insert_chunk", "doc1", "a.txt", 1, "h\u00e9llo \"quoted\"\n", None, [1.0, -0.5, 1e-07], None),
    op("insert_chunk", "doc/x", "n", 2, "", 0, [0.0], {}),
], [J(200, CREATED), J(200, CREATED), J(200, CREATED)])
HITS = {"hits": {"hits": [
    {"_id": "doc1:0", "_score": 0.912345, "_source": {"text": "first \u00e9", "document_id": "doc1", "document_name": "a.txt", "chunk_index": 0, "embedding": [0.1, 0.2]}},
    {"_id": "doc1:1", "_score": 0.12345, "_source": {"text": "second", "document_id": "doc1", "document_name": "a.txt", "chunk_index": 1}},
    {"_id": "doc2:0", "_score": 0.99995, "_source": {"document_id": "doc2", "document_name": "b.txt"}},
    {"_id": "doc3:0", "_score": 0.00005, "_source": {"text": "t", "document_id": "doc3", "document_name": "c", "chunk_index": "7"}},
    {"_id": "doc4:0", "_score": 1, "_source": {"text": "", "document_id": None, "document_name": None, "chunk_index": 2}},
    {"_id": "doc5:0", "_score": 0, "_source": {"text": "z", "document_id": "doc5", "document_name": "e", "chunk_index": 2.0}},
    {"_id": "doc6:0", "_score": 0.55555, "_source": {"text": "y", "document_id": "doc6", "document_name": "f", "chunk_index": 3}},
    {"_id": "doc7:0", "_score": 0.88885, "_source": {"text": "x", "document_id": "doc7", "document_name": "g", "chunk_index": 4}},
]}}
case("search_plain", [op("search_dense", [0.1, 0.2], 3, None)], [J(200, HITS)])
case("search_filtered", [op("search_dense", [0.1, 0.2], 5, {"document_id": "doc1", "chunk_index": 2, "tenant_id": "acme"})],
     [J(200, {"hits": {"hits": []}})])
case("search_empty_embedding_and_sparse_flag", [op("search_dense", [], 3, None), op("supports_sparse")], [])
case("search_error_500", [op("search_dense", [0.5], 2, None)], [J(500, {"error": "boom"})])
case("list_documents", [op("list_documents", 10, 5)], [
    J(200, {"hits": {"hits": [
        {"_id": "doc1", "_source": {"filename": "a.txt", "metadata": "{\"a\": 1, \"u\": \"\\u00e9\"}", "uploaded_at": "2026-10-19T10:00:00.123456+00:00"}},
        {"_id": "doc:2", "_source": {"filename": "b.txt"}},
    ]}}),
    J(200, {"count": 3}),
    J(200, {}),
])
case("list_documents_empty", [op("list_documents", 10, 0)], [J(200, {"hits": {"hits": []}})])
case("delete_ok", [op("delete_document", "doc1")], [J(200, {"deleted": 3}), J(200, {"result": "deleted"})])
case("delete_registry_404", [op("delete_document", "doc1")], [J(200, {"deleted": 0}), J(404, {"result": "not_found"})])
case("delete_registry_500", [op("delete_document", "doc1")], [J(200, {"deleted": 0}), J(500, {"error": "x"})])
case("delete_by_query_500", [op("delete_document", "doc1")], [J(500, {"error": "boom"})])
case("get_filename_variants", [op("get_document_filename", "doc1") for _ in range(5)], [
    J(200, {"_source": {"filename": "a \u00e9.txt"}}),
    J(404, {"found": False}),
    J(200, {"_source": {}}),
    J(500, {"error": "x"}),
    J(200, {"found": True}),
])
case("basic_auth", [op("insert_document", "d", "f.txt", {}), op("get_document_filename", "d")],
     [J(200, CREATED), J(200, {"_source": {"filename": "f.txt"}})], ctor={"http_auth": ["user", "pw"]})

case("init_head_500", [op("init_schema", 8)], [J(500, {"error": "x"})])
case("init_get_mapping_500", [op("init_schema", 8)], [J(200), J(500, {"error": "x"})])
case("insert_created_201", [
    op("insert_document", "d1", "f.txt", {}),
    op("insert_chunk", "d1", "f.txt", 0, "t", 1, [0.5], {}),
], [J(201, CREATED), J(201, CREATED)])
case("insert_error_500", [op("insert_chunk", "d1", "f.txt", 0, "t", 1, [0.5], {})], [J(500, {"error": "x"})])
case("search_index_missing_404", [op("search_dense", [0.5], 2, None)], [J(404, {"error": "index_not_found"})])
case("count_error_in_list", [op("list_documents", 5, 0)], [
    J(200, {"hits": {"hits": [{"_id": "d1", "_source": {"filename": "f"}}]}}), J(500, {"error": "x"})])
case("search_filter_value_types", [op("search_dense", [1.0], 2, {"document_id": None, "chunk_index": [1, 2], "token_count": 1.5, "document_name": "x"})],
     [J(200, {"hits": {"hits": []}})])

case("empty_ids", [
    op("insert_document", "", "f.txt", {}),
    op("get_document_filename", ""),
    op("delete_document", ""),
], [J(200, CREATED), J(200, {"deleted": 0})])
case("search_empty_filter_dict", [op("search_dense", [1.0], 2, {})], [J(200, {"hits": {"hits": []}})])

case("search_unknown_filter_keys", [
    op("search_dense", [1.0], 2, {"flag": True}),
    op("search_dense", [1.0], 2, {"document_id": "d", "a": 1, "b": 2}),
    op("search_dense", [1.0], 2, {"tenant_id": "acme"}),
    op("search_dense", [], 2, {"flag": True}),
], [J(200, {"hits": {"hits": []}})])
case("insert_chunk_tenant_id", [
    op("insert_chunk", "d1", "a.txt", 0, "t", 1, [0.5], {"tenant_id": "acme"}),
    op("insert_chunk", "d1", "a.txt", 1, "t", 1, [0.5], {"tenant_id": 7, "page": 2}),
    op("insert_chunk", "d1", "a.txt", 2, "t", 1, [0.5], {"tenant_id": None}),
    op("insert_chunk", "d1", "a.txt", 3, "t", 1, [0.5], {"tenant_id": ""}),
    op("insert_chunk", "d1", "a.txt", 4, "t", 1, [0.5], {"tenant_id": True}),
    op("insert_chunk", "d1", "a.txt", 5, "t", 1, [0.5], {"tenant_id": 1.5}),
], [J(200, CREATED)] * 6)


def make(ctor):
    kw = {}
    if "index_name" in ctor:
        kw["index_name"] = ctor["index_name"]
    if ctor.get("documents_index_name"):
        kw["documents_index_name"] = ctor["documents_index_name"]
    if ctor.get("http_auth"):
        kw["http_auth"] = tuple(ctor["http_auth"])
    return mod.OpenSearchBackend("http://127.0.0.1:%d" % PORT, **kw)


def run_ops(be, ops):
    out = []
    for o in ops:
        try:
            out.append({"ok": getattr(be, o["call"])(*o["args"])})
        except TransportError:
            out.append({"error": "TransportError"})
        except Exception as e:  # noqa: BLE001
            out.append({"error": type(e).__name__, "message": str(e)})
    return out


for c in cases:
    fake.script = [list(x) for x in c["script"]]
    fake.log = []
    fake.unscripted = 0
    c["results"] = run_ops(make(c["ctor"]), c["ops"])
    c["log"] = fake.log
    assert fake.unscripted == 0, (c["name"], "unscripted request")
    assert not fake.script, (c["name"], "scripted answers left over", len(fake.script))

ctor_cases = []
for url in ["", None]:
    try:
        mod.OpenSearchBackend(url)
        res = {"ok": True}
    except Exception as e:  # noqa: BLE001
        res = {"error": type(e).__name__, "message": str(e)}
    ctor_cases.append({"url": url, "result": res})

parse_urls = [
    "http://localhost:9200", "https://search.example.com:9443/some/path", "localhost", "localhost:9300",
    "http://example.com/path", "http://[::1]:9200", "http://user:pw@host:9200", "http://h: 9200",
    "http://h:+9200", "http://h:92_00", "http://h:abc", "http://h:", "http://:9200", "", " ",
    "a://b://c:1", "HTTP://UP:1", "http://h:9200?x=1:2", "http://h:\u0669\u0662\u0660\u0660", "http://h:9200 ",
]

parse_urls += [
    "http://h:-5", "http://h: 12 ", "http://h:1__2", "http://h:_1", "http://h:1_", "http://h:0x10",
    "http://h:\uff11\uff12", "http://h:\uff11_\uff12", "http://h:" + "9" * 30, "http://h:\u0663",
    "http://h:1e3", "http://h:\t9200\n", "http://h:9200:1", "http://h:\u00b2", "http://h:\u2003",
    "http://h:\u00a09200", "http://h:\u200b9200", "http://h:1_0", "http://h:0", "http://h:00",
]
parse_cases = []
for url in parse_urls:
    try:
        host, port = mod.OpenSearchBackend._parse_url(url)
        res = {"ok": [host, str(port)]}
    except Exception as e:  # noqa: BLE001
        res = {"error": type(e).__name__}
    parse_cases.append({"url": url, "result": res})

os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "opensearch_cases.json"), "w", encoding="utf-8") as f:
    json.dump({"pin": PIN, "python": sys.version.split()[0], "opensearch_py": opensearchpy.__versionstr__,
               "cases": cases, "ctor_cases": ctor_cases, "parse_cases": parse_cases}, f)

total = 0
for c in cases:
    total += len(c["log"])
    print("##", c["name"], "| requests:", len(c["log"]), "|", json.dumps(c["results"])[:105])
    for r in c["log"]:
        q = "&".join("=".join(x) for x in r["query"])
        b = json.dumps(r["body"])[:95] if r["body"] is not None else ""
        print("   ", r["method"], r["path"] + ("?" + q if q else ""), b, "AUTH" if r["authorization"] else "")
print("cases:", len(cases), "requests:", total, "ctor_cases:", len(ctor_cases), "parse_cases:", len(parse_cases))
for p in parse_cases:
    print("   parse", json.dumps(p["url"]), "->", json.dumps(p["result"]))
print("opensearch-py", opensearchpy.__versionstr__, "python", sys.version.split()[0])
