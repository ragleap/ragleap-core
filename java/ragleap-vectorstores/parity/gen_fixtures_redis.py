"""Recorded-command parity generator for RedisBackend (ragleap-vectorstores).

Runs the REAL pinned Python backend with the real redis-py client (forced to RESP2 with ?protocol=2) against a
fake RESP server that logs every command (arguments as hex) and answers from a scripted queue; the SQLite
document registry is real (fresh temp file per scenario). The Java test replays the same scripts against its own
fake RESP server and compares the commands and the results. Handshake commands (HELLO, CLIENT) are not compared.
Not live-verified: no real Redis Stack.

Needs: git archive <pin> packages/ragleap-vectorstores | tar -x -C /root/py-vs-pinned
       venv with redis, numpy (/root/venv-vs)
"""
import datetime
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import types

SRC = "/root/py-vs-pinned/packages/ragleap-vectorstores/src/ragleap_vectorstores/redis_backend.py"
OUT = os.environ.get("RAGLEAP_VS_PARITY_DIR", "/root/fixtures-vs")
PIN = os.environ.get("VS_PIN", "unknown")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?$")

for _name in ("ragleap", "ragleap.vectorstores", "ragleap.vectorstores.base"):
    sys.modules[_name] = types.ModuleType(_name)


class VectorBackend:
    pass


sys.modules["ragleap.vectorstores.base"].VectorBackend = VectorBackend

spec = importlib.util.spec_from_file_location("redis_backend_pinned", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.__file__ == SRC, "wrong module path"

import redis  # noqa: E402
from redis.exceptions import RedisError  # noqa: E402


class Fake:
    script = []
    log = []
    handshake = []
    unscripted = 0


fake = Fake()
all_handshake = set()


def enc(s):
    t = s[0]
    if t == "s":
        return b"+" + s[1].encode() + b"\r\n"
    if t == "e":
        return b"-" + s[1].encode() + b"\r\n"
    if t == "i":
        return b":" + str(s[1]).encode() + b"\r\n"
    if t == "n":
        return b"$-1\r\n"
    if t == "b":
        d = s[1].encode("utf-8")
        return b"$" + str(len(d)).encode() + b"\r\n" + d + b"\r\n"
    if t == "a":
        return b"*" + str(len(s[1])).encode() + b"\r\n" + b"".join(enc(x) for x in s[1])
    raise ValueError(t)


def handle(c):
    f = c.makefile("rb")
    while True:
        line = f.readline()
        if not line or not line.startswith(b"*"):
            return
        n = int(line[1:])
        args = []
        for _ in range(n):
            ln = int(f.readline()[1:])
            args.append(f.read(ln))
            f.read(2)
        cmd = args[0].upper()
        if cmd in (b"HELLO", b"CLIENT"):
            h = tuple(a.decode("latin1") for a in args)
            fake.handshake.append(h)
            all_handshake.add(h)
            c.sendall(b"-ERR unknown command\r\n" if cmd == b"HELLO" else b"+OK\r\n")
            continue
        fake.log.append({"cmd": [a.hex() for a in args]})
        if fake.script:
            reply = fake.script.pop(0)
        else:
            fake.unscripted += 1
            reply = ["e", "ERR unscripted"]
        c.sendall(enc(reply))


srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(16)
PORT = srv.getsockname()[1]
URL = "redis://127.0.0.1:%d/0?protocol=2" % PORT


def serve():
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(c,), daemon=True).start()


threading.Thread(target=serve, daemon=True).start()

OKR = ["s", "OK"]
NIL = ["n"]


def E(m):
    return ["e", m]


def I(n):
    return ["i", n]


def B(x):
    return ["b", x]


def A(*xs):
    return ["a", list(xs)]


def mods(*names):
    return A(*[A(B("name"), B(n), B("ver"), I(21020), B("path"), B("/x/%s.so" % n), B("args"), A()) for n in names])


def FT(total, *docs):
    items = [I(total)]
    for key, fields in docs:
        items.append(B(key))
        items.append(A(*[B(x) for pair in fields for x in pair]))
    return A(*items)


INFO_OK = A(B("index_name"), B("ragleap_idx"))
INIT = [mods("search"), INFO_OK]
INFO_TENANT = A(B("index_name"), B("ragleap_idx"), B("attributes"),
                A(A(B("identifier"), B("tenant_id"), B("attribute"), B("tenant_id"), B("type"), B("TAG"))))
INIT_T = [mods("search"), INFO_TENANT]
P = "ragleap:ragleap_idx:chunk:"
cases = []


def case(name, ops, script, ctor=None):
    cases.append({"name": name, "ctor": ctor or {}, "ops": ops, "script": script})


def op(call, *args):
    return {"call": call, "args": list(args)}


def hit(key, text, score, did, dname, cidx):
    return (key, [("text", text), ("score", score), ("document_id", did), ("document_name", dname), ("chunk_index", cidx)])


case("init_fresh_creates_index", [op("init_schema", 8)], [mods("search"), E("Unknown Index name"), OKR])
case("init_existing_index_reused_without_dimension_check", [op("init_schema", 1536)], INIT)
case("init_two_modules", [op("init_schema", 8)], [mods("ReJSON", "search"), INFO_OK])
case("init_no_search_module", [op("init_schema", 8)], [mods("ReJSON")])
case("init_module_list_empty", [op("init_schema", 8)], [A()])
case("init_module_list_error", [op("init_schema", 8)], [E("ERR unknown command 'MODULE'")])
case("init_create_error", [op("init_schema", 8)], [mods("search"), E("Unknown Index name"), E("Index already exists")])
case("init_custom_names_and_prefix", [op("init_schema", 3)],
     [mods("search"), E("no such index"), OKR], ctor={"index_name": "my idx", "key_prefix": "kp:"})
case("init_default_prefix_from_index_name", [op("init_schema", 3)],
     [mods("search"), E("no such index"), OKR], ctor={"index_name": "Weird idx"})
case("registry_before_init", [
    op("insert_document", "doc1", "a.txt", {"k": "v", "n": 1, "u": "\u00e9", "nested": {"a": [1, None, True]}}),
    op("insert_document", "doc:2", "b \u00e9.txt", None),
    op("insert_document", "d3", "c.txt", {}),
    op("list_documents", 10, 0),
    op("list_documents", 1, 1),
    op("get_document_filename", "doc:2"),
    op("get_document_filename", "nope"),
    op("delete_document", "doc1"),
    op("delete_document", "ghost"),
    op("supports_sparse"),
], [])
case("insert_document_duplicate", [
    op("insert_document", "doc1", "a.txt", {}), op("insert_document", "doc1", "other.txt", {}),
    op("get_document_filename", "doc1"),
], [])
case("chunk_and_search_before_init", [
    op("insert_chunk", "doc1", "a.txt", 0, "t", 1, [0.1], {}),
    op("search_dense", [0.1, 0.2], 3, None),
], [])
case("insert_chunk_variants", [
    op("init_schema", 8),
    op("insert_chunk", "doc1", "a.txt", 0, "hello", 5, [0.1, 0.2, 0.3], {"page": 1}),
    op("insert_chunk", "doc1", "a.txt", 1, "h\u00e9llo \"q\"\n", None, [1.0, -0.5, 1e-07, 1e39, -0.0], None),
    op("insert_chunk", "doc:x", "n", 2, "keep", 3, [0.0], {"text": "x", "extra": {"a": [1, 2]}}),
    op("insert_chunk", "", "n", 0, "", 0, [], {}),
], INIT + [I(7)] * 4)
case("insert_chunk_error", [op("init_schema", 8), op("insert_chunk", "d", "n", 0, "t", 1, [0.5], {})], INIT + [E("OOM command not allowed")])
HITS = FT(
    8,
    hit(P + "doc1:0", "first \u00e9", "0.087655", "doc1", "a.txt", "0"),
    hit(P + "doc1:1", "second", "0.87655", "doc1", "a.txt", "1"),
    hit(P + "doc2:0", "", "0.00005", "doc2", "b.txt", "2"),
    hit(P + "doc3:0", "t", "1", "doc3", "c", "3"),
    hit(P + "doc4:0", "z", "2.0", "doc4", "d", "4"),
    hit(P + "doc5:0", "y", "-0.00001", "doc5", "e", "5"),
    hit(P + "doc6:0", "x", "0.44445", "doc6", "f", "6"),
    hit(P + "doc7:0", "w", "0.11115", "doc7", "g", " 3 "),
)
case("search_plain", [op("init_schema", 8), op("search_dense", [0.1, 0.2], 3, None)], INIT + [HITS])
SPECIAL = "a-b.c d{e}\\f\u00e9_ok:1"
case("search_filters", [
    op("init_schema", 8),
    op("search_dense", [1.0], 2, {"document_id": SPECIAL}),
    op("search_dense", [1.0], 2, {"other": "ignored", "n": 5}),
    op("search_dense", [1.0], 2, {"document_id": "plain", "other": "x"}),
    op("search_dense", [1.0], 2, {"document_id": 5}),
    op("search_dense", [1.0], 2, {}),
    op("search_dense", [1.0], 0, None),
    op("search_dense", [], 2, {"document_id": "x"}),
], INIT + [FT(0)] * 4)
case("search_error", [op("init_schema", 8), op("search_dense", [0.5], 2, None)], INIT + [E("Syntax error at offset 1")])
case("search_doc_missing_field", [op("init_schema", 8), op("search_dense", [0.5], 2, None)],
     INIT + [FT(1, (P + "d:0", [("text", "t"), ("score", "0.1"), ("document_id", "d"), ("chunk_index", "0")]))])
case("search_doc_bad_score", [op("init_schema", 8), op("search_dense", [0.5], 2, None)],
     INIT + [FT(1, hit(P + "d:0", "t", "abc", "d", "n", "0"))])
case("list_with_index", [
    op("init_schema", 8),
    op("insert_document", "d1", "a.txt", {"a": 1}),
    op("insert_document", "d:2", "b.txt", {}),
    op("insert_document", "a-b.c", "c.txt", {}),
    op("list_documents", 10, 0),
    op("list_documents", 1, 1),
], INIT + [A(I(2)), A(I(3)), A(I(0)), A(I(5))])
case("list_search_error", [
    op("init_schema", 8), op("insert_document", "d1", "a.txt", {}), op("list_documents", 5, 0),
], INIT + [E("Unknown index name")])
case("delete_sequence", [
    op("init_schema", 8),
    op("insert_document", "d1", "a.txt", {}),
    op("insert_document", "d:2", "b.txt", {}),
    op("delete_document", "d1"),
    op("get_document_filename", "d1"),
    op("delete_document", "d1"),
    op("delete_document", "ghost"),
    op("delete_document", "d:2"),
], INIT + [
    FT(2, (P + "d1:0", [("text", "x")]), (P + "d1:1", [("text", "y")])), I(2),
    FT(0),
    FT(0),
    FT(1, (P + "d:2:0", [("text", "z")])), I(1),
])
case("delete_search_error_keeps_registry", [
    op("init_schema", 8), op("insert_document", "d1", "a.txt", {}),
    op("delete_document", "d1"), op("get_document_filename", "d1"),
], INIT + [E("Unknown index name")])
case("delete_del_error", [
    op("init_schema", 8), op("insert_document", "d1", "a.txt", {}), op("delete_document", "d1"),
    op("get_document_filename", "d1"),
], INIT + [FT(1, (P + "d1:0", [("text", "x")])), E("READONLY You can't write against a read only replica")])


case("search_score_edge_values", [op("init_schema", 8), op("search_dense", [0.5, 0.5], 8, None)], INIT + [FT(
    5,
    hit(P + "e:0", "a", "nan", "e", "n", "0"),
    hit(P + "e:1", "b", "inf", "e", "n", "1"),
    hit(P + "e:2", "c", "-inf", "e", "n", "2"),
    hit(P + "e:3", "d", "2.0000002", "e", "n", "3"),
    hit(P + "e:4", "e", "-0.0000001", "e", "n", "4"),
)])


case("search_tenant_filters", [
    op("init_schema", 8),
    op("search_dense", [1.0], 2, {"tenant_id": "acme"}),
    op("search_dense", [1.0], 2, {"document_id": "d1", "tenant_id": "acme"}),
    op("search_dense", [1.0], 2, {"tenant_id": "acme", "document_id": SPECIAL}),
    op("search_dense", [1.0], 2, {"tenant_id": SPECIAL}),
    op("search_dense", [1.0], 2, {"tenant_id": 5}),
    op("search_dense", [1.0], 2, {"tenant_id": "acme", "other": "x"}),
    op("search_dense", [1.0], 2, {"other": "x"}),
], INIT_T + [FT(0)] * 5)
case("search_tenant_filter_on_index_without_field", [
    op("init_schema", 8),
    op("search_dense", [1.0], 2, {"tenant_id": "acme"}),
    op("search_dense", [1.0], 2, {"other": "x", "tenant_id": "acme"}),
    op("search_dense", [1.0], 2, {"document_id": "d1"}),
], INIT + [FT(0)])
case("search_tenant_filter_on_fresh_index", [
    op("init_schema", 8),
    op("search_dense", [1.0], 2, {"tenant_id": "acme"}),
], [mods("search"), E("Unknown Index name"), OKR, FT(0)])
case("insert_chunk_tenant_id", [
    op("init_schema", 8),
    op("insert_chunk", "d1", "a.txt", 0, "t", 1, [0.5], {"tenant_id": "acme"}),
    op("insert_chunk", "d1", "a.txt", 1, "t", 1, [0.5], {"tenant_id": 7, "page": 2}),
    op("insert_chunk", "d1", "a.txt", 2, "t", 1, [0.5], {"tenant_id": None}),
    op("insert_chunk", "d1", "a.txt", 3, "t", 1, [0.5], {"tenant_id": ""}),
    op("insert_chunk", "d1", "a.txt", 4, "t", 1, [0.5], {"tenant_id": True}),
    op("insert_chunk", "d1", "a.txt", 5, "t", 1, [0.5], {"tenant_id": 1.5}),
], INIT_T + [I(8)] * 6)


def classify(e):
    if isinstance(e, RedisError):
        return {"error": "RemoteError"}
    if isinstance(e, sqlite3.IntegrityError):
        return {"error": "IntegrityError"}
    if isinstance(e, RuntimeError):
        return {"error": "RuntimeError", "message": str(e).replace(URL, "<url>")}
    if isinstance(e, AttributeError):
        return {"error": "NotInitialized" if "NoneType" in str(e) else "AttributeError"}
    if isinstance(e, ValueError):
        r = {"error": "ValueError"}
        if "can only filter" in str(e):
            r["message"] = str(e)
        return r
    return {"error": type(e).__name__, "message": str(e)[:120]}


def jsonable(v):
    if isinstance(v, bytes):
        return v.decode("utf-8")
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    return v


raw_types = {}


def run_ops(be, ops, name):
    out = []
    for o in ops:
        try:
            res = getattr(be, o["call"])(*o["args"])
            if o["call"] == "list_documents":
                for d in res:
                    assert ISO.match(d["uploaded_at"]), d["uploaded_at"]
                    d["uploaded_at"] = "<iso-naive>"
            if o["call"] == "search_dense" and res and name not in raw_types:
                raw_types[name] = {k: type(v).__name__ for k, v in res[0].items()}
            out.append({"ok": jsonable(res)})
        except Exception as e:  # noqa: BLE001
            out.append(classify(e))
    return out


tmp = tempfile.mkdtemp(prefix="vs-redis-")
for i, c in enumerate(cases):
    fake.script = [list(x) for x in c["script"]]
    fake.log = []
    fake.unscripted = 0
    kw = {}
    if "index_name" in c["ctor"]:
        kw["index_name"] = c["ctor"]["index_name"]
    if "key_prefix" in c["ctor"]:
        kw["key_prefix"] = c["ctor"]["key_prefix"]
    be = mod.RedisBackend(URL, registry_path=os.path.join(tmp, "reg%d.sqlite3" % i), **kw)
    c["results"] = run_ops(be, c["ops"], c["name"])
    c["log"] = fake.log
    be._conn.close()
    assert fake.unscripted == 0, (c["name"], "unscripted command", fake.log[-1:])
    assert not fake.script, (c["name"], "scripted replies left over", len(fake.script))
shutil.rmtree(tmp, ignore_errors=True)

ctor_cases = []
for url in ["", None]:
    try:
        mod.RedisBackend(url, registry_path=os.path.join(tempfile.gettempdir(), "x-redis-ctor.sqlite3"))
        res = {"ok": True}
    except Exception as e:  # noqa: BLE001
        res = {"error": type(e).__name__, "message": str(e)}
    ctor_cases.append({"url": url, "result": res})

os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "redis_cases.json"), "w", encoding="utf-8") as f:
    json.dump({"pin": PIN, "python": sys.version.split()[0], "redis_py": redis.__version__, "cases": cases,
               "ctor_cases": ctor_cases}, f)


def show(cmd_hex):
    parts = []
    for h in cmd_hex:
        b = bytes.fromhex(h)
        try:
            s = b.decode("utf-8")
            parts.append(s if s.isprintable() else "<%d bytes>" % len(b))
        except UnicodeDecodeError:
            parts.append("<%d bytes>" % len(b))
    return " ".join(parts)


total = 0
for c in cases:
    total += len(c["log"])
    print("##", c["name"], "| commands:", len(c["log"]), "|", json.dumps(c["results"], ensure_ascii=False)[:110])
    for r in c["log"]:
        print("   ", show(r["cmd"])[:215])
print("cases:", len(cases), "commands:", total, "ctor_cases:", len(ctor_cases))
print("handshake commands seen:", sorted(all_handshake))
print("raw result types:", raw_types)
print("redis-py", redis.__version__, "python", sys.version.split()[0])
