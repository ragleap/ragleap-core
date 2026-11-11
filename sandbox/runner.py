"""
RagLeap code-sandbox runner. Stdlib only, deliberately tiny.

This is the ONE component that holds the Docker socket (effectively root on
the host), so it accepts no user-controlled container options: every run
uses the fixed spec in build_spec(). Model code arrives as JSON, is passed to
the container through an environment variable (never a shell or command
line), and runs in a fresh container with: no network, read-only root, all
capabilities dropped, non-root user, memory/cpu/pids/time limits, a small
noexec tmpfs, capped logs. The container is deleted after every run.
Auth: Authorization: Bearer $SANDBOX_TOKEN. One run at a time (else 429).

Owner-granted file access (off unless COMPUTER_CONTROL=true): the owner lists folders in a grants
file on the host that only this runner can read; the app and the API cannot create or change a
grant. POST /files runs a fixed reader in a fresh no-network container with ONE granted folder
mounted read-only. GET /grants lists grant names and modes, never host paths.
POST /stage (grants with purpose "knowledge" only) copies allowed documents from the read-only folder into a
staging volume that the app mounts read-only, so the app can ingest them without touching the host.
"""
import hmac
import http.client
import json
import os
import posixpath
import re
import socket
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
IMAGE = os.environ.get("SANDBOX_IMAGE", "python:3.11-slim")
TOKEN = os.environ.get("SANDBOX_TOKEN", "")
PORT = int(os.environ.get("SANDBOX_PORT", "8099"))
TIMEOUT_SECONDS = max(1, min(30, int(os.environ.get("SANDBOX_TIMEOUT", "10"))))
MEMORY_BYTES = 256 * 1024 * 1024
MAX_CODE_CHARS = 8000
MAX_OUTPUT_BYTES = 16 * 1024
MAX_BODY = 16 * 1024
LABEL = "ragleap.sandbox"
BOOTSTRAP = "import os;exec(compile(os.environ.pop('CODE'),'<sandbox>','exec'))"
SHELL_BOOTSTRAP = 'c="$CMD"; unset CMD; eval "$c"'


class _Unix(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def _docker(method, path, body=None, timeout=30):
    c = _Unix(DOCKER_SOCKET, timeout)
    try:
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data,
                  headers={"Content-Type": "application/json"} if data is not None else {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def build_spec(code, mode="python"):
    """The ONLY container spec ever used. Nothing here comes from the caller except the code."""
    return {
        "Image": IMAGE,
        "Cmd": (["sh", "-c", SHELL_BOOTSTRAP] if mode == "shell"
                else ["python", "-I", "-B", "-u", "-c", BOOTSTRAP]),
        "Env": [("CMD=" if mode == "shell" else "CODE=") + code],
        "User": "65534:65534",
        "WorkingDir": "/tmp",
        "NetworkDisabled": True,
        "Labels": {LABEL: "1"},
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Privileged": False,
            "Memory": MEMORY_BYTES,
            "MemorySwap": MEMORY_BYTES,
            "NanoCpus": 500000000,
            "PidsLimit": 64,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m"},
            "Ulimits": [{"Name": "nofile", "Soft": 64, "Hard": 64},
                        {"Name": "fsize", "Soft": 16777216, "Hard": 16777216}],
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "100k", "max-file": "1"}},
        },
    }


def demux(buf):
    """Split Docker's multiplexed (non-TTY) log stream into (stdout, stderr)."""
    out, err, i = bytearray(), bytearray(), 0
    while i + 8 <= len(buf):
        stream = buf[i]
        size = int.from_bytes(buf[i + 4:i + 8], "big")
        chunk = buf[i + 8:i + 8 + size]
        i += 8 + size
        (err if stream == 2 else out).extend(chunk)
    return bytes(out), bytes(err)


def run_in_sandbox(code, mode="python"):
    return _run_spec(build_spec(code, mode))


def _run_spec(spec, timeout=None):
    limit = timeout or TIMEOUT_SECONDS
    cid = None
    try:
        st, body = _docker("POST", "/containers/create", spec)
        if st != 201:
            raise RuntimeError(f"create failed: HTTP {st} {body[:200]!r} (is {IMAGE} pulled?)")
        cid = json.loads(body)["Id"]
        st, _ = _docker("POST", f"/containers/{cid}/start")
        if st not in (204, 304):
            raise RuntimeError(f"start failed: HTTP {st}")
        start, killed = time.time(), False
        while True:
            _, body = _docker("GET", f"/containers/{cid}/json")
            state = json.loads(body)["State"]
            if not state.get("Running"):
                break
            elapsed = time.time() - start
            if elapsed > limit and not killed:
                _docker("POST", f"/containers/{cid}/kill")
                killed = True
            if elapsed > limit + 5:
                break
            time.sleep(0.2)
        _, logs = _docker("GET", f"/containers/{cid}/logs?stdout=1&stderr=1")
        out, err = demux(logs)
        return {
            "exit_code": state.get("ExitCode"),
            "stdout": out[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"),
            "stderr": err[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"),
            "timed_out": killed,
            "oom": bool(state.get("OOMKilled")),
        }
    finally:
        if cid:
            try:
                _docker("DELETE", f"/containers/{cid}?force=true&v=true")
            except Exception:
                pass


def cleanup_orphans():
    try:
        flt = urllib.parse.quote(json.dumps({"label": [LABEL + "=1"]}))
        _, body = _docker("GET", f"/containers/json?all=1&filters={flt}")
        for c in json.loads(body):
            _docker("DELETE", f"/containers/{c['Id']}?force=true&v=true")
    except Exception as e:
        print(f"orphan cleanup skipped: {e}", flush=True)


# ---------------------------------------------------------------------------
# Owner-granted file access. Off unless COMPUTER_CONTROL=true. Version 1 can list and read only.
# ---------------------------------------------------------------------------
GRANTS_FILE = os.environ.get("SANDBOX_GRANTS_FILE", "/grants/grants.json")
FILE_READ_BYTES = 12000
FILE_LIST_MAX = 100
MAX_REL_CHARS = 300
FILE_OPS = ("list", "read", "preview")
PREVIEW_TEXT_MAX = 8000      # characters of proposed text a preview accepts (a request body is capped at 16 KiB)
PREVIEW_OLD_MAX = 200000     # bytes of the existing file a preview will read
PREVIEW_DIFF_LINES = 200
PREVIEW_DIFF_CHARS = 6000
GRANT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
DENY_NAMES = frozenset({".env", ".netrc", ".pgpass", ".git-credentials", ".htpasswd", "credentials",
                        "credentials.json", ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".git"})
DENY_PREFIXES = (".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
DENY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")
SYSTEM_PATHS = ("/etc", "/proc", "/sys", "/dev", "/boot", "/run", "/var/run", "/var/lib/docker",
                "/usr", "/bin", "/sbin", "/lib", "/lib64")
BROAD_PATHS = frozenset({"/", "/root", "/home", "/Users", "/var", "/tmp", "/opt", "/mnt", "/srv", "/media"})


def computer_control_on():
    """On only when COMPUTER_CONTROL=true AND the owner has not paused it with `ragleap files off`,
    which creates a DISABLED file next to the grants. The flag is checked on every call, so it is immediate."""
    if os.environ.get("COMPUTER_CONTROL", "").strip().lower() != "true":
        return False
    return not os.path.exists(os.path.join(os.path.dirname(GRANTS_FILE), "DISABLED"))


def name_denied(name):
    n = name.lower()
    return n in DENY_NAMES or n.startswith(DENY_PREFIXES) or n.endswith(DENY_SUFFIXES)


def grant_path_problem(path):
    """Why a host folder may not be granted, or None. The owner's command checks this too."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or "\\" in path or len(path) > 500:
        return "the folder must be an absolute path"
    if posixpath.normpath(path) != path:
        return "write the path without .., . or a trailing slash"
    if path in BROAD_PATHS or re.fullmatch(r"/(home|Users)/[^/]+", path):
        return "that folder is too broad"
    for s in SYSTEM_PATHS:
        if path == s or path.startswith(s + "/"):
            return "that is a system folder"
    if any(name_denied(p) for p in path.split("/") if p):
        return "the path contains a protected name"
    return None


def parse_grant(g):
    """A cleaned grant, or None with the reason. Never includes anything the file did not say."""
    if not isinstance(g, dict):
        return None, "not an object"
    name, path, mode = g.get("name"), g.get("path"), g.get("mode")
    if not isinstance(name, str) or not GRANT_NAME_RE.fullmatch(name):
        return None, "bad name"
    if mode not in ("read", "write"):
        return None, "mode must be read or write"
    problem = grant_path_problem(path)
    if problem:
        return None, problem
    purpose = g.get("purpose", "use")
    if purpose not in ("use", "knowledge"):
        return None, "bad purpose"
    expires, when = g.get("expires"), None
    if expires is not None:
        try:
            when = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except ValueError:
            return None, "bad expiry"
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    return {"name": name, "path": path, "mode": mode, "purpose": purpose, "expires": expires, "_when": when}, None


def load_grants():
    try:
        with open(GRANTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"grants file unreadable: {type(e).__name__}", flush=True)
        return []
    items = data.get("grants") if isinstance(data, dict) else None
    out, seen = [], set()
    for raw in (items if isinstance(items, list) else []):
        g, why = parse_grant(raw)
        if g is None or g["name"] in seen:
            print("grant skipped: " + (why or "duplicate name"), flush=True)
            continue
        seen.add(g["name"])
        out.append(g)
    return out


def active_grants(now=None):
    now = now or datetime.now(timezone.utc)
    return [g for g in load_grants() if g["_when"] is None or g["_when"] > now]


def grants_summary():
    """Names and modes only: the model and the dashboard never see a host path."""
    if not computer_control_on():
        return {"enabled": False, "grants": []}
    return {"enabled": True,
            "grants": [{"name": g["name"], "mode": g["mode"], "purpose": g["purpose"], "expires": g["expires"]}
                       for g in active_grants()]}


def rel_problem(rel, allow_empty=False):
    if not isinstance(rel, str):
        return "path must be text"
    if not rel.strip():
        return None if allow_empty else "give a file path"
    if len(rel) > MAX_REL_CHARS or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return "that path is not allowed"
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." or name_denied(p) for p in parts):
        return "that path is not allowed"
    return None


# The reader runs INSIDE the container, so a symlink resolves against the container's own filesystem
# and cannot reach other host files. It also refuses anything that resolves outside /data, and any
# protected name, even when reached through a symlink.
_READER_TEMPLATE = r"""
import hashlib, json, os, sys
DN = set(__NAMES__)
DP = tuple(__PREFIXES__)
DS = tuple(__SUFFIXES__)
BASE = os.path.realpath(os.environ.get("RL_DATA", "/data"))
OP = os.environ.get("OP", "")
REL = os.environ.get("REL", "")
MAXB = int(os.environ.get("MAXB", "12000"))
MAXN = int(os.environ.get("MAXN", "100"))
OLDMAX = int(os.environ.get("OLDMAX", "200000"))
DIFFL = int(os.environ.get("DIFFL", "200"))
DIFFC = int(os.environ.get("DIFFC", "6000"))

def out(obj, code=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)

def denied(n):
    n = n.lower()
    return n in DN or n.startswith(DP) or n.endswith(DS)

try:
    parts = [p for p in REL.split("/") if p not in ("", ".")]
    if any(p == ".." or denied(p) for p in parts):
        out({"error": "that path is not allowed"}, 3)
    target = os.path.realpath(os.path.join(BASE, *parts)) if parts else BASE
    if target != BASE and not target.startswith(BASE + os.sep):
        out({"error": "that path is outside the granted folder"}, 3)
    back = os.path.relpath(target, BASE)
    if back != "." and any(denied(p) for p in back.split(os.sep)):
        out({"error": "that path is not allowed"}, 3)
    if OP == "list":
        if not os.path.isdir(target):
            out({"error": "not a folder"}, 3)
        entries, truncated = [], False
        for e in sorted(os.scandir(target), key=lambda x: x.name):
            if denied(e.name) or e.is_symlink():
                continue
            if len(entries) >= MAXN:
                truncated = True
                break
            try:
                if e.is_dir(follow_symlinks=False):
                    entries.append({"name": e.name[:200], "type": "dir"})
                elif e.is_file(follow_symlinks=False):
                    entries.append({"name": e.name[:200], "type": "file", "size": e.stat(follow_symlinks=False).st_size})
            except OSError:
                continue
        out({"op": "list", "entries": entries, "truncated": truncated})
    if OP == "read":
        if not os.path.isfile(target):
            out({"error": "not a file"}, 3)
        size = os.stat(target).st_size
        with open(target, "rb") as f:
            data = f.read(MAXB)
        if b"\x00" in data[:2048]:
            out({"op": "read", "binary": True, "size": size})
        res = {"op": "read", "size": size, "bytes_read": len(data), "truncated": size > len(data),
               "sha256": hashlib.sha256(data).hexdigest(), "text": data.decode("utf-8", "replace")}
        while len(json.dumps(res, ensure_ascii=False).encode("utf-8")) > 15000 and res["text"]:
            res["text"] = res["text"][: len(res["text"]) // 2]
            res["truncated"] = True
        out(res)
    if OP == "preview":
        # Read-only: shows what writing NEW to this file would change. Nothing is written.
        import difflib
        new_text = os.environ.get("NEW", "")
        if target == BASE or os.path.isdir(target):
            out({"error": "not a file"}, 3)
        old_b = None
        old_text = ""
        if os.path.exists(target):
            if not os.path.isfile(target):
                out({"error": "not a file"}, 3)
            if os.stat(target).st_size > OLDMAX:
                out({"error": "that file is too large to preview (limit %d bytes)" % OLDMAX}, 3)
            with open(target, "rb") as f:
                old_b = f.read(OLDMAX + 1)
            if len(old_b) > OLDMAX:
                out({"error": "that file is too large to preview (limit %d bytes)" % OLDMAX}, 3)
            if b"\x00" in old_b[:2048]:
                out({"error": "that is a binary file"}, 3)
            try:
                old_text = old_b.decode("utf-8")
            except UnicodeDecodeError:
                out({"error": "that file is not UTF-8 text"}, 3)
        elif not os.path.isdir(os.path.dirname(target)):
            out({"error": "the folder does not exist"}, 3)
        added_nl = False
        if old_text.endswith("\n") and new_text and not new_text.endswith("\n"):
            new_text += "\n"
            added_nl = True
        new_b = new_text.encode("utf-8")
        shown_rel = "".join(ch if ch.isprintable() else "?" for ch in REL)[:200]
        diff = list(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                         ("a/" + shown_rel) if old_b is not None else "/dev/null", "b/" + shown_rel, n=2))
        body = diff[2:]
        added = sum(1 for ln in body if ln.startswith("+"))
        removed = sum(1 for ln in body if ln.startswith("-"))
        text = "".join(ln if ln.endswith("\n") else ln + "\n" for ln in diff[:DIFFL])
        cut = len(diff) > DIFFL
        if len(text) > DIFFC:
            text, cut = text[:DIFFC], True
        out({"op": "preview", "exists": old_b is not None, "old_size": None if old_b is None else len(old_b),
             "new_size": len(new_b), "old_sha256": None if old_b is None else hashlib.sha256(old_b).hexdigest(),
             "new_sha256": hashlib.sha256(new_b).hexdigest(), "identical": old_b is not None and old_b == new_b,
             "added_final_newline": added_nl, "added": added, "removed": removed, "diff": text, "diff_truncated": cut})
    out({"error": "unknown operation"}, 2)
except OSError as e:
    out({"error": "could not read: " + e.__class__.__name__}, 3)
"""
FILE_READER = (_READER_TEMPLATE
               .replace("__NAMES__", json.dumps(sorted(DENY_NAMES)))
               .replace("__PREFIXES__", json.dumps(list(DENY_PREFIXES)))
               .replace("__SUFFIXES__", json.dumps(list(DENY_SUFFIXES))))


def build_file_spec(grant, op, rel, text=None):
    """build_spec() plus exactly one read-only bind mount of the granted folder. Nothing else changes."""
    spec = build_spec("")
    spec["Cmd"] = ["python", "-I", "-B", "-c", FILE_READER]
    spec["Env"] = ["OP=" + op, "REL=" + rel, "MAXB=%d" % FILE_READ_BYTES, "MAXN=%d" % FILE_LIST_MAX]
    if op == "preview":
        spec["Env"] += ["NEW=" + (text or ""), "OLDMAX=%d" % PREVIEW_OLD_MAX, "DIFFL=%d" % PREVIEW_DIFF_LINES,
                        "DIFFC=%d" % PREVIEW_DIFF_CHARS]
    spec["HostConfig"]["Mounts"] = [{"Type": "bind", "Source": grant["path"], "Target": "/data", "ReadOnly": True}]
    return spec


def file_op(payload):
    """Returns (http_status, body). The grant decides what is mounted; the caller only names it."""
    if not computer_control_on():
        return 403, {"error": "computer control is off"}
    if not isinstance(payload, dict):
        return 400, {"error": "body must be a JSON object"}
    op, name, rel = payload.get("op"), payload.get("grant"), payload.get("path", "")
    if op not in FILE_OPS:
        return 400, {"error": "op must be one of: " + ", ".join(FILE_OPS)}
    if not isinstance(name, str):
        return 400, {"error": "grant must be a name"}
    problem = rel_problem(rel, allow_empty=(op == "list"))
    if problem:
        return 400, {"error": problem}
    text = payload.get("text")
    if op == "preview" and (not isinstance(text, str) or "\x00" in text or len(text) > PREVIEW_TEXT_MAX):
        return 400, {"error": "text must be text, at most %d characters" % PREVIEW_TEXT_MAX}
    grant = next((g for g in active_grants() if g["name"] == name), None)
    if grant is None:
        return 404, {"error": "unknown or expired grant"}
    if op == "preview" and grant["mode"] != "write":
        return 403, {"error": "this grant is read-only"}
    res = _run_spec(build_file_spec(grant, op, rel, text if op == "preview" else None))
    try:
        parsed = json.loads(res.get("stdout") or "")
    except ValueError:
        parsed = None
    ok = isinstance(parsed, dict) and "error" not in parsed and res.get("exit_code") == 0 \
        and not res.get("timed_out") and not res.get("oom")
    print(json.dumps({"audit": "file", "grant": name, "op": op, "path": rel[:120], "ok": ok,
                      "bytes": (parsed or {}).get("bytes_read") if isinstance(parsed, dict) else None}), flush=True)
    if ok:
        return 200, {"ok": True, "result": parsed}
    if res.get("timed_out"):
        msg = "timed out"
    elif res.get("oom"):
        msg = "out of memory"
    elif isinstance(parsed, dict) and parsed.get("error"):
        msg = str(parsed["error"])
    else:
        msg = "could not read (exit %s)" % res.get("exit_code")
    return 200, {"ok": False, "error": msg[:200]}


# ---------------------------------------------------------------------------
# Folder staging for automatic ingestion. Only grants with purpose "knowledge" are staged, and only
# when computer control is on. A fixed copier copies allowed files from the read-only granted folder
# into a named staging volume; the app mounts that volume read-only and never sees a host path.
# ---------------------------------------------------------------------------
STAGING_VOLUME = "ragleap_core_staging"
STAGE_TIMEOUT = 120
STAGE_MAX_FILE = 25 * 1024 * 1024
STAGE_MAX_COPIES = 200
STAGE_MAX_FILES = 5000
STAGE_MAX_BYTES = 500 * 1024 * 1024
STAGE_EXTENSIONS = (".csv", ".docx", ".eml", ".epub", ".htm", ".html", ".json", ".md", ".odp", ".ods", ".odt",
                    ".parquet", ".pdf", ".pptx", ".rtf", ".sql", ".srt", ".tsv", ".txt", ".vtt", ".xls", ".xlsx",
                    ".xml", ".xsl", ".xslt", ".yaml", ".yml", ".zip")

# Runs INSIDE a no-network container as uid 65534, like the reader. Symlinks are never followed or copied,
# protected names are skipped, and every path is built from names found in the granted folder.
_STAGER_TEMPLATE = r"""
import hashlib, json, os, stat, sys, time
DN = set(__NAMES__)
DP = tuple(__PREFIXES__)
DS = tuple(__SUFFIXES__)
EXTS = tuple(__EXTS__)
BASE = os.path.realpath(os.environ.get("RL_DATA", "/data"))
ROOT = os.environ.get("RL_STAGING", "/staging")
GRANT = os.environ.get("GRANT", "")
MAXF = int(os.environ.get("MAXF", "26214400"))
MAXC = int(os.environ.get("MAXC", "200"))
MAXN = int(os.environ.get("MAXN", "5000"))
MAXT = int(os.environ.get("MAXT", "524288000"))
SCAN_MAX, DEPTH_MAX, REL_MAX = 200000, 20, 300
OUT = os.path.join(ROOT, GRANT)
MAN = os.path.join(ROOT, ".manifests")
os.umask(0o022)

def denied(n):
    n = n.lower()
    return n in DN or n.startswith(DP) or n.endswith(DS)

def stop(msg, code=3):
    sys.stdout.write(json.dumps({"error": msg}))
    sys.exit(code)

if not GRANT or "/" in GRANT or GRANT.startswith("."):
    stop("bad grant name", 2)
skipped = {"extension": 0, "too_large": 0, "denied": 0, "symlink": 0, "special": 0, "too_long": 0, "unreadable": 0}
try:
    prev = {}
    try:
        with open(os.path.join(MAN, GRANT + ".json"), "r", encoding="utf-8") as f:
            for e in json.load(f).get("files", []):
                prev[e["path"]] = e
    except Exception:
        prev = {}
    cands, seen, truncated, stack = [], 0, False, [("", 0)]
    while stack and not truncated:
        rel_dir, depth = stack.pop()
        try:
            entries = sorted(os.scandir(os.path.join(BASE, rel_dir) if rel_dir else BASE), key=lambda x: x.name)
        except OSError:
            skipped["unreadable"] += 1
            continue
        for e in entries:
            seen += 1
            if seen > SCAN_MAX:
                truncated = True
                break
            if denied(e.name):
                skipped["denied"] += 1
                continue
            if e.is_symlink():
                skipped["symlink"] += 1
                continue
            rel = rel_dir + "/" + e.name if rel_dir else e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < DEPTH_MAX:
                        stack.append((rel, depth + 1))
                    continue
                if not e.is_file(follow_symlinks=False):
                    skipped["special"] += 1
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                skipped["unreadable"] += 1
                continue
            if len(rel) > REL_MAX:
                skipped["too_long"] += 1
            elif not e.name.lower().endswith(EXTS):
                skipped["extension"] += 1
            elif st.st_size > MAXF:
                skipped["too_large"] += 1
            else:
                cands.append((rel, st.st_size, int(st.st_mtime)))
    os.makedirs(OUT, mode=0o755, exist_ok=True)

    def copy_one(rel):
        src, dest, tmp = os.path.join(BASE, rel), os.path.join(OUT, rel), None
        try:
            fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            skipped["unreadable"] += 1
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                skipped["special"] += 1
                return None
            os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
            tmp = os.path.join(os.path.dirname(dest), ".stage-%d.tmp" % os.getpid())
            h, n = hashlib.sha256(), 0
            with os.fdopen(fd, "rb", closefd=False) as fin, open(tmp, "wb") as fout:
                while True:
                    chunk = fin.read(1 << 20)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > MAXF:
                        raise ValueError("file grew while copying")
                    h.update(chunk)
                    fout.write(chunk)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
            return {"path": rel, "size": n, "mtime": int(st.st_mtime), "sha256": h.hexdigest()}
        except (OSError, ValueError):
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            skipped["unreadable"] += 1
            return None
        finally:
            os.close(fd)

    cands.sort()
    keep, copies, total, pending = {}, 0, 0, 0
    for rel, size, mtime in cands:
        old, dest = prev.get(rel), os.path.join(OUT, rel)
        staged = os.path.isfile(dest) and not os.path.islink(dest)
        if len(keep) >= MAXN or total + size > MAXT:
            truncated = True
        elif old and staged and old.get("size") == size and old.get("mtime") == mtime and os.path.getsize(dest) == size:
            keep[rel] = old
            total += size
        elif copies >= MAXC:
            pending += 1
            if old and staged:
                keep[rel] = old
                total += old.get("size", 0)
        else:
            ent = copy_one(rel)
            if ent:
                keep[rel] = ent
                total += ent["size"]
                copies += 1
    removed = 0
    for root, dirs, files in os.walk(OUT, topdown=False, followlinks=False):
        for fn in files:
            p = os.path.join(root, fn)
            if os.path.relpath(p, OUT).replace(os.sep, "/") not in keep:
                try:
                    os.unlink(p)
                    removed += 1
                except OSError:
                    pass
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                if os.path.islink(dp):
                    os.unlink(dp)
                elif not os.listdir(dp):
                    os.rmdir(dp)
            except OSError:
                pass
    os.makedirs(MAN, mode=0o755, exist_ok=True)
    doc = {"grant": GRANT, "generated_at": int(time.time()), "files": [keep[k] for k in sorted(keep)],
           "skipped": skipped, "pending": pending, "truncated": truncated}
    tmpm = os.path.join(MAN, "." + GRANT + ".tmp")
    with open(tmpm, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.chmod(tmpm, 0o644)
    os.replace(tmpm, os.path.join(MAN, GRANT + ".json"))
    sys.stdout.write(json.dumps({"op": "stage", "grant": GRANT, "files": len(keep), "copied": copies, "removed": removed,
                                 "bytes": total, "pending": pending, "truncated": truncated, "skipped": skipped}))
except OSError as e:
    stop("could not stage: " + e.__class__.__name__)
"""
STAGER = (_STAGER_TEMPLATE
          .replace("__NAMES__", json.dumps(sorted(DENY_NAMES)))
          .replace("__PREFIXES__", json.dumps(list(DENY_PREFIXES)))
          .replace("__SUFFIXES__", json.dumps(list(DENY_SUFFIXES)))
          .replace("__EXTS__", json.dumps(list(STAGE_EXTENSIONS))))

# Removes staged folders and manifests of grants that are gone, expired or no longer "knowledge".
PRUNER = r"""
import json, os, shutil, sys
KEEP = set(json.loads(os.environ.get("KEEP", "[]")))
ROOT = os.environ.get("RL_STAGING", "/staging")
removed = []
try:
    for name in sorted(os.listdir(ROOT)):
        if name == ".manifests" or name in KEEP:
            continue
        p = os.path.join(ROOT, name)
        if os.path.islink(p) or os.path.isfile(p):
            os.unlink(p)
        else:
            shutil.rmtree(p, ignore_errors=True)
        removed.append(name[:40])
    man = os.path.join(ROOT, ".manifests")
    if os.path.isdir(man):
        for fn in sorted(os.listdir(man)):
            if not fn.endswith(".json") or fn[:-5] not in KEEP:
                try:
                    os.unlink(os.path.join(man, fn))
                except OSError:
                    pass
    sys.stdout.write(json.dumps({"op": "prune", "removed": removed}))
except OSError as e:
    sys.stdout.write(json.dumps({"error": "could not prune: " + e.__class__.__name__}))
    sys.exit(3)
"""
# A new empty named volume belongs to root; this one fixed step makes its top folder writable (sticky) for the
# unprivileged copier. It runs as the owner of the folder with every capability dropped.
PREPARE = "import os;os.chmod('/staging',0o1777)"


def _staging_mount():
    return {"Type": "volume", "Source": STAGING_VOLUME, "Target": "/staging", "ReadOnly": False,
            "VolumeOptions": {"NoCopy": True}}


def build_stage_spec(grant):
    """build_spec() plus ONE read-only bind mount of the granted folder and the staging volume. The caller
    chooses nothing except which grant (by name), and the grant decides the folder."""
    spec = build_spec("")
    spec["Cmd"] = ["python", "-I", "-B", "-c", STAGER]
    spec["Env"] = ["GRANT=" + grant["name"], "MAXF=%d" % STAGE_MAX_FILE, "MAXC=%d" % STAGE_MAX_COPIES,
                   "MAXN=%d" % STAGE_MAX_FILES, "MAXT=%d" % STAGE_MAX_BYTES]
    host = spec["HostConfig"]
    host["Mounts"] = [{"Type": "bind", "Source": grant["path"], "Target": "/data", "ReadOnly": True}, _staging_mount()]
    host["Ulimits"] = [{"Name": "nofile", "Soft": 64, "Hard": 64},
                       {"Name": "fsize", "Soft": STAGE_MAX_FILE + 1048576, "Hard": STAGE_MAX_FILE + 1048576}]
    return spec


def build_prune_spec(keep):
    spec = build_spec("")
    spec["Cmd"] = ["python", "-I", "-B", "-c", PRUNER]
    spec["Env"] = ["KEEP=" + json.dumps(sorted(keep))]
    spec["HostConfig"]["Mounts"] = [_staging_mount()]
    return spec


def build_prepare_spec():
    spec = build_spec("")
    spec["Cmd"] = ["python", "-I", "-B", "-c", PREPARE]
    spec["Env"] = []
    spec["User"] = "0:0"
    spec["HostConfig"]["Mounts"] = [_staging_mount()]
    return spec


def _summary(res):
    """(parsed dict or None, error text or None) from one finished container."""
    try:
        parsed = json.loads(res.get("stdout") or "")
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and "error" not in parsed and res.get("exit_code") == 0 \
            and not res.get("timed_out") and not res.get("oom"):
        return parsed, None
    if res.get("timed_out"):
        return None, "timed out"
    if res.get("oom"):
        return None, "out of memory"
    if isinstance(parsed, dict) and parsed.get("error"):
        return None, str(parsed["error"])[:200]
    return None, "failed (exit %s)" % res.get("exit_code")


def stage_op(payload):
    """Returns (http_status, body). Stages every active knowledge grant, or just the one named."""
    if not computer_control_on():
        return 403, {"error": "computer control is off"}
    if not isinstance(payload, dict):
        return 400, {"error": "body must be a JSON object"}
    name = payload.get("grant")
    if name is not None and not isinstance(name, str):
        return 400, {"error": "grant must be a name"}
    knowledge = [g for g in active_grants() if g["purpose"] == "knowledge"]
    targets = knowledge if name is None else [g for g in knowledge if g["name"] == name]
    if name is not None and not targets:
        return 404, {"error": "unknown, expired or not a knowledge grant"}
    prep = _run_spec(build_prepare_spec(), timeout=30)
    if prep.get("exit_code") != 0 or prep.get("timed_out"):
        print(json.dumps({"audit": "stage", "ok": False, "step": "prepare"}), flush=True)
        return 200, {"ok": False, "error": "the staging volume is not ready"}
    results, ok = [], True
    for g in targets:
        parsed, err = _summary(_run_spec(build_stage_spec(g), timeout=STAGE_TIMEOUT))
        ok = ok and parsed is not None
        print(json.dumps({"audit": "stage", "grant": g["name"], "ok": parsed is not None,
                          "files": (parsed or {}).get("files"), "copied": (parsed or {}).get("copied")}), flush=True)
        results.append(dict(parsed, ok=True) if parsed else {"grant": g["name"], "ok": False, "error": err})
    pruned, err = _summary(_run_spec(build_prune_spec([g["name"] for g in knowledge]), timeout=60))
    ok = ok and pruned is not None
    return 200, {"ok": ok, "results": results, "pruned": (pruned or {}).get("removed", []),
                 **({} if pruned else {"prune_error": err})}


_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *a):  # never log request bodies or code
        pass

    def _reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        want = ("Bearer " + TOKEN).encode()
        return hmac.compare_digest(self.headers.get("Authorization", "").encode(), want)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, {"status": "ok"})
        if self.path == "/grants":
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            return self._reply(200, grants_summary())
        self._reply(404, {"error": "not found"})

    def _files(self, length):
        try:
            payload = json.loads(self.rfile.read(length))
        except Exception:
            return self._reply(400, {"error": "body must be JSON"})
        if not _lock.acquire(blocking=False):
            return self._reply(429, {"error": "busy"})
        try:
            code, obj = file_op(payload)
            self._reply(code, obj)
        except Exception as e:
            print(f"file op failed: {e}", flush=True)
            self._reply(500, {"error": "sandbox error"})
        finally:
            _lock.release()

    def _stage(self, length):
        try:
            payload = json.loads(self.rfile.read(length))
        except Exception:
            return self._reply(400, {"error": "body must be JSON"})
        if not _lock.acquire(blocking=False):
            return self._reply(429, {"error": "busy"})
        try:
            code, obj = stage_op(payload)
        except Exception as e:
            print(f"stage failed: {e}", flush=True)
            code, obj = 500, {"error": "sandbox error"}
        finally:
            _lock.release()      # released before replying, so a client's next call is never refused as busy
        self._reply(code, obj)

    def do_POST(self):
        if self.path not in ("/run", "/files", "/stage"):
            return self._reply(404, {"error": "not found"})
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._reply(413, {"error": "bad body size"})
        if self.path == "/files":
            return self._files(length)
        if self.path == "/stage":
            return self._stage(length)
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or ("code" in payload) == ("shell" in payload):
                raise ValueError  # exactly one of "code" / "shell"
            key = "shell" if "shell" in payload else "code"
            code = payload[key]
            if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE_CHARS or "\x00" in code:
                raise ValueError
        except Exception:
            return self._reply(400, {"error": "body must be JSON {\"code\": \"<=8000 chars\"}"})
        if not _lock.acquire(blocking=False):
            return self._reply(429, {"error": "busy"})
        try:
            self._reply(200, run_in_sandbox(code, "shell") if key == "shell" else run_in_sandbox(code))
        except Exception as e:
            print(f"run failed: {e}", flush=True)
            self._reply(500, {"error": "sandbox error"})
        finally:
            _lock.release()


def main():
    if not TOKEN:
        print("SANDBOX_TOKEN is not set; refusing to start.", flush=True)
        sys.exit(1)
    cleanup_orphans()
    print("computer control: " + ("ON, grants loaded: %d" % len(active_grants()) if computer_control_on() else "off"), flush=True)
    print(f"sandbox runner listening on :{PORT} (image {IMAGE}, timeout {TIMEOUT_SECONDS}s)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
