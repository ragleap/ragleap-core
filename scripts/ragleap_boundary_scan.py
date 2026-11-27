#!/usr/bin/env python3
"""Static boundary scan for ragleap-core (Phase 0 of the capability contract).

Two checks, both AST-based, so comments and strings never trigger them:

1. EXECUTORS: only core/autonomy.py may reference the functions that act on the outside world (send, run, fetch,
   call a tool, touch files) or start a sub-agent (subagents.run_delegation), and inside autonomy.py the senders may
   appear only in _send_via_channel and run_delegation only in execute_delegation.
2. CONNECTORS: code under core/integrations must not use the network, the environment or a subprocess directly;
   it must go through HostServices (a later phase). Today's legacy uses are listed in a baseline file.

Usage: python scripts/ragleap_boundary_scan.py [--root .] [--baseline tests/data/integration_egress_baseline.json]
Exit code 0 means clean, 1 means a violation or a stale baseline entry.
"""
import argparse
import ast
import json
import pathlib
import sys

# Functions that act on the outside world, by module. Configuration helpers (enabled, grant_names ...) are not listed.
EXECUTORS = {
    "action_senders": {"send_webhook", "send_slack", "send_email", "send_crm_update", "send_calendar_event"},
    "code_exec": {"run_code", "run_shell"},
    "page_fetch": {"fetch_page"},
    "mcp_client": {"call_tool"},
    "file_access": {"list_files", "read_file", "preview_change", "write_file", "delete_file", "preview_raw", "stat_raw"},
    "subagents": {"run_delegation"},      # starting a child run is an execution path too (P1-C)
}
EXECUTOR_HOME = {"core/autonomy.py"}
SENDER_ONLY_IN = "_send_via_channel"   # inside autonomy.py the action senders may appear only in this function
SENDER_MODULES = {"action_senders", "code_exec", "page_fetch", "mcp_client"}
DELEGATION_MODULES = {"subagents"}
DELEGATION_ONLY_IN = "execute_delegation"   # inside autonomy.py run_delegation may appear only in this function
# Named exceptions (module file, executor module, function) with the reason.
EXECUTOR_EXCEPTIONS = {
    ("core/folder_ingest.py", "file_access", "_base"): "reads granted folders for ingestion through the runner (FOLDER_INGEST_ENABLED, read-only, not an agent action)",
    ("core/folder_ingest.py", "file_access", "_headers"): "same as above",
}
# Executor-module files themselves are not scanned for Part 1.
# core/subagents.py is NOT listed: it is a normal module that must not reach the other executors either, so it stays scanned.
EXECUTOR_FILES = {f"core/{m}.py" for m in EXECUTORS if m != "subagents"}

NETWORK_MODULES = {"requests", "httpx", "aiohttp", "socket", "subprocess"}
NETWORK_DOTTED = {"urllib.request", "http.client"}
DYNAMIC_IMPORTS = {"__import__", "import_module"}
WATCH_DRIVERS = {"psycopg2", "pymysql", "mysql", "pymongo", "snowflake", "google.cloud", "pyodbc", "oracledb"}


def _parse(path: pathlib.Path):
    return ast.parse(path.read_text(encoding="utf-8"))


def scan_executors(source: str, rel: str):
    """Return [(rel, line, module, function, enclosing_function)] for every executor reference outside the home."""
    tree = ast.parse(source)
    aliases = {}      # local name -> (module, function or None)
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.module == "core":
                for a in n.names:
                    if a.name in EXECUTORS:
                        aliases[a.asname or a.name] = (a.name, None)
            elif n.module and n.module.startswith("core.") and n.module[5:] in EXECUTORS:
                mod = n.module[5:]
                for a in n.names:
                    aliases[a.asname or a.name] = (mod, a.name)
    found = []

    def walk(node, enclosing):
        for child in ast.iter_child_nodes(node):
            enc = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else enclosing
            if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name) and child.value.id in aliases:
                mod, fn = aliases[child.value.id]
                if fn is None:
                    found.append((rel, child.lineno, mod, child.attr, enclosing))
            elif isinstance(child, ast.Name) and child.id in aliases and aliases[child.id][1] is not None:
                mod, fn = aliases[child.id]
                found.append((rel, child.lineno, mod, fn, enclosing))
            walk(child, enc)

    walk(tree, None)
    return [f for f in found if f[3] in EXECUTORS[f[2]] or (f[0], f[2], f[3]) in EXECUTOR_EXCEPTIONS]


def executor_violations(root: pathlib.Path):
    out = []
    for base in ("core", "channels", "scripts", "sandbox"):
        for p in sorted((root / base).rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if rel in EXECUTOR_FILES or rel == "scripts/ragleap_boundary_scan.py":
                continue
            try:
                refs = scan_executors(p.read_text(encoding="utf-8"), rel)
            except SyntaxError:
                continue
            for (r, line, mod, fn, enclosing) in refs:
                if (r, mod, fn) in EXECUTOR_EXCEPTIONS:
                    continue
                if r in EXECUTOR_HOME:
                    if mod in SENDER_MODULES and enclosing != SENDER_ONLY_IN:
                        out.append(f"{r}:{line} {mod}.{fn} is used outside {SENDER_ONLY_IN} (in {enclosing})")
                    if mod in DELEGATION_MODULES and enclosing != DELEGATION_ONLY_IN:
                        out.append(f"{r}:{line} {mod}.{fn} is used outside {DELEGATION_ONLY_IN} (in {enclosing})")
                    continue
                out.append(f"{r}:{line} {mod}.{fn} must only be used from core/autonomy.py")
    return out


def scan_connector(source: str):
    """Return (violations, watch) for one connector file. violations: sorted unique 'requests', 'os.environ:KEY', ..."""
    tree = ast.parse(source)
    viol, watch = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                top = a.name.split(".")[0]
                if top in NETWORK_MODULES or a.name in NETWORK_DOTTED:
                    viol.add(a.name.split(".")[0] if top in NETWORK_MODULES else a.name)
                if top in WATCH_DRIVERS or a.name.startswith("google.cloud"):
                    watch.add(top)
        elif isinstance(n, ast.ImportFrom) and n.module:
            top = n.module.split(".")[0]
            if top in NETWORK_MODULES:
                viol.add(top)
            elif n.module in NETWORK_DOTTED or any(n.module == d for d in NETWORK_DOTTED):
                viol.add(n.module)
            elif n.module == "urllib" and any(a.name == "request" for a in n.names):
                viol.add("urllib.request")
            elif n.module == "http" and any(a.name == "client" for a in n.names):
                viol.add("http.client")
            elif n.module == "os" and any(a.name in ("environ", "getenv") for a in n.names):
                viol.add("os.environ:<imported name>")
            elif n.module == "importlib" and any(a.name == "import_module" for a in n.names):
                viol.add("dynamic-import")
            if top in WATCH_DRIVERS or n.module.startswith("google.cloud"):
                watch.add(top)
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os" and n.attr in ("environ", "getenv"):
            viol.add("os.environ:<dynamic>")   # refined below for constant keys
        elif isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
            if name in DYNAMIC_IMPORTS:
                viol.add("dynamic-import")
    # refine os.environ uses: constant key names are reported as os.environ:KEY, anything else stays <dynamic>
    keys, dynamic = set(), False
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.args:
            f = n.func
            is_env_get = (isinstance(f.value, ast.Attribute) and isinstance(f.value.value, ast.Name) and f.value.value.id == "os"
                          and f.value.attr == "environ" and f.attr in ("get", "pop", "setdefault"))
            is_getenv = isinstance(f.value, ast.Name) and f.value.id == "os" and f.attr == "getenv"
            if is_env_get or is_getenv:
                a0 = n.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    keys.add(a0.value)
                else:
                    dynamic = True
        elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and isinstance(n.value.value, ast.Name) \
                and n.value.value.id == "os" and n.value.attr == "environ":
            sl = n.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                keys.add(sl.value)
            else:
                dynamic = True
    if "os.environ:<dynamic>" in viol:
        viol.discard("os.environ:<dynamic>")
        if dynamic or not keys:
            viol.add("os.environ:<dynamic>")
        for k in keys:
            viol.add("os.environ:" + k)
    return sorted(viol), sorted(watch)


def connector_report(root: pathlib.Path):
    rep, watch = {}, {}
    for p in sorted((root / "core" / "integrations").rglob("*.py")):
        rel = p.relative_to(root / "core" / "integrations").as_posix()
        v, w = scan_connector(p.read_text(encoding="utf-8"))
        if v:
            rep[rel] = v
        if w:
            watch[rel] = w
    return rep, watch


def check_connectors(root: pathlib.Path, baseline: dict):
    """Compare the scan with the baseline. Returns a list of problems (empty means clean)."""
    rep, _ = connector_report(root)
    allowed = {}
    for f, items in baseline.get("legacy_connectors", {}).items():
        allowed.setdefault(f, set()).update(items)
    for f, envs in baseline.get("host_side", {}).items():
        allowed.setdefault(f, set()).update("os.environ:" + k for k in envs.get("os.environ", []))
    problems = []
    for f, items in rep.items():
        for it in items:
            if it not in allowed.get(f, set()):
                problems.append(f"core/integrations/{f}: {it} is not allowed here: use HostServices (or, for a legacy file, it must be in the baseline)")
    for f, items in allowed.items():
        for it in sorted(items):
            if it not in rep.get(f, []):
                problems.append(f"baseline entry no longer needed, remove it: core/integrations/{f}: {it}")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--baseline", default="tests/data/integration_egress_baseline.json")
    a = ap.parse_args(argv)
    root = pathlib.Path(a.root).resolve()
    baseline = json.loads((root / a.baseline).read_text(encoding="utf-8"))
    problems = executor_violations(root) + check_connectors(root, baseline)
    for p in problems:
        print("VIOLATION:", p)
    print("boundary scan:", "clean" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
