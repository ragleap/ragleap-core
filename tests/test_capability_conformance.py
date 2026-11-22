"""Capability schema, local checker and docs (Phase 0, P0-B): published together and kept consistent."""
import copy
import glob
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest

import ragleap_conformance as conf

SCHEMA_PATH = ROOT / "docs" / "capability-schema-v1.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
EXAMPLES = sorted(glob.glob(str(ROOT / "docs" / "capability-examples" / "*.json")))
DOC = (ROOT / "docs" / "capability-schema.md").read_text(encoding="utf-8")
BASE = json.loads((ROOT / "docs" / "capability-examples" / "crm-action.json").read_text(encoding="utf-8"))


def codes(decl):
    return {c for _, c, _ in conf.check_declaration(decl, SCHEMA)}


def levels(decl, level):
    return {c for l, c, _ in conf.check_declaration(decl, SCHEMA) if l == level}


# ---------- the published files ----------

def test_the_schema_is_valid_json_with_the_expected_identity():
    assert SCHEMA["$id"] == "urn:ragleap:capability:1"
    assert SCHEMA["properties"]["schema"]["const"] == "ragleap.capability/1"
    assert set(SCHEMA["required"]) == {"schema", "id", "version", "kind", "operations"}


@pytest.mark.parametrize("path", EXAMPLES)
def test_every_published_example_conforms(path):
    decl = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert levels(decl, "error") == set()


def test_there_are_exactly_the_three_documented_examples_and_the_doc_lists_them():
    names = {os.path.basename(p) for p in EXAMPLES}
    assert names == {"crm-action.json", "sql-datasource.json", "mcp-server.json"}
    for n in names:
        assert n in DOC


def test_the_docs_use_the_draft_status_and_the_exact_trust_statement():
    assert "Status: in design (draft)" in DOC
    assert "they do not contain a malicious in-process connector" in DOC
    assert "Nothing in the RagLeap core reads a declaration yet" in DOC


def test_every_finding_code_the_checker_can_emit_is_documented():
    source = (ROOT / "scripts" / "ragleap_conformance.py").read_text(encoding="utf-8")
    source += (ROOT / "core" / "connector_host" / "schema.py").read_text(encoding="utf-8")   # the shared rules live here
    emitted = set(re.findall(r'"([EWI]-[A-Z0-9-]+)"', source))
    assert emitted, "no codes found"
    for code in emitted:
        assert f"`{code}`" in DOC, f"{code} is not documented"


def test_the_schema_covers_the_real_taint_sets():
    # fetch_page and mcp_call are in both OUTBOUND_TOOLS and TAINTING_TOOLS, so 'both' must exist
    assert "both" in SCHEMA["$defs"]["operation"]["properties"]["taint"]["enum"]


# ---------- negative cases: each rule has a test ----------

def mutate(fn):
    d = copy.deepcopy(BASE)
    fn(d)
    return d


CASES = [
    ("unknown top-level field", lambda d: d.update(extra=1), "E-SCHEMA"),
    ("missing version", lambda d: d.pop("version"), "E-SCHEMA"),
    ("bad id", lambda d: d.update(id="NoDots"), "E-SCHEMA"),
    ("bad kind", lambda d: d.update(kind="plugin"), "E-SCHEMA"),
    ("wrong schema string", lambda d: d.update(schema="ragleap.capability/2"), "E-SCHEMA"),
    ("no operations", lambda d: d.update(operations=[]), "E-SCHEMA"),
    ("bad effect", lambda d: d["operations"][0].update(effect="destroy"), "E-SCHEMA"),
    ("bad taint", lambda d: d["operations"][0].update(taint="maybe"), "E-SCHEMA"),
    ("wildcard host", lambda d: d["network"].update(hosts=["*.example.com"]), "E-SCHEMA"),
    ("https_only false", lambda d: d["network"].update(https_only=False), "E-SCHEMA"),
    ("bad port", lambda d: d["network"].update(tcp=[{"host": "db.example.com", "port": 70000}]), "E-SCHEMA"),
    ("budget too large", lambda d: d["budgets"].update(max_calls_per_run=100000), "E-SCHEMA"),
    ("lower-case secret", lambda d: d.update(secrets=["token"]), "E-SCHEMA"),
    ("unknown field inside an operation", lambda d: d["operations"][0].update(color="red"), "E-SCHEMA"),
    ("approval never", lambda d: d["operations"][0].update(approval="never"), "E-APPROVAL-NEVER"),
    ("duplicate operation names", lambda d: d["operations"].append(dict(d["operations"][0])), "E-OP-DUPLICATE"),
    ("user scope without a field", lambda d: d.update(scope={"level": "user"}), "E-SCOPE-USER-FIELD"),
    ("protected secret", lambda d: d.update(secrets=["DATABASE_URL"]), "E-SECRET-PROTECTED"),
    ("encryption key as a secret", lambda d: d.update(secrets=["ADDON_ENCRYPTION_KEY"]), "E-SECRET-PROTECTED"),
    ("host is an IP address", lambda d: d["network"].update(hosts=["10.0.0.5"]), "E-NET-INTERNAL-HOST"),
    ("host is an internal name", lambda d: d["network"].update(hosts=["erp.internal"]), "E-NET-INTERNAL-HOST"),
    ("localhost host", lambda d: d["network"].update(hosts=["localhost"]), "E-NET-INTERNAL-HOST"),
    ("external_send without a network", lambda d: d.pop("network"), "E-NET-REQUIRED"),
]


@pytest.mark.parametrize("name,fn,code", CASES, ids=[c[0] for c in CASES])
def test_a_broken_declaration_is_reported_with_its_code(name, fn, code):
    assert code in levels(mutate(fn), "error")


def test_the_unmodified_base_has_no_errors():
    assert levels(BASE, "error") == set()


def test_tcp_gives_a_warning_not_an_error():
    d = mutate(lambda d: d["network"].update(tcp=[{"host": "db.example.com", "port": 5432}]))
    assert "W-TCP-NOT-ENFORCEABLE" in levels(d, "warning") and levels(d, "error") == set()


def test_data_classes_are_descriptive_only():
    plain = mutate(lambda d: d["descriptive"].pop("data_classes"))
    secret = mutate(lambda d: d["descriptive"].update(data_classes=["credentials", "financial"]))
    weird = mutate(lambda d: d["descriptive"].update(data_classes=["madeup"]))
    assert levels(plain, "error") == levels(secret, "error") == levels(weird, "error") == set()
    assert "W-DATA-CLASS-UNKNOWN" in levels(weird, "warning")
    assert "I-DESCRIPTIVE-ONLY" in levels(secret, "info")


# ---------- the validator agrees with a full JSON Schema implementation when one is installed ----------

def test_the_built_in_validator_agrees_with_jsonschema_on_every_case():
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft202012Validator(SCHEMA)
    samples = [json.loads(pathlib.Path(p).read_text(encoding="utf-8")) for p in EXAMPLES]
    for name, fn, code in CASES:
        samples.append(mutate(fn))
    for s in samples:
        assert (not conf.validate(s, SCHEMA)) == v.is_valid(s), json.dumps(s)[:120]


# ---------- the command line ----------

def run(*args):
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "ragleap_conformance.py"), *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def test_the_command_line_exit_codes(tmp_path):
    good = ROOT / "docs" / "capability-examples" / "crm-action.json"
    assert run(str(good))[0] == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "ragleap.capability/1"}))
    rc, out, _ = run(str(bad))
    assert rc == 1 and "E-SCHEMA" in out
    assert run(str(tmp_path / "missing.json"))[0] == 2
    notjson = tmp_path / "x.json"
    notjson.write_text("{not json")
    assert run(str(notjson))[0] == 2
    assert run()[0] == 2


def test_strict_makes_a_warning_fail_and_json_output_parses():
    sql = str(ROOT / "docs" / "capability-examples" / "sql-datasource.json")
    assert run(sql)[0] == 0
    assert run(sql, "--strict")[0] == 1
    rc, out, _ = run(sql, "--json")
    data = json.loads(out)
    assert data[0]["file"] == sql and any(f["code"] == "W-TCP-NOT-ENFORCEABLE" for f in data[0]["findings"])


def test_print_schema_matches_the_file():
    rc, out, _ = run("--print-schema")
    assert rc == 0 and json.loads(out) == SCHEMA


def test_source_lint_flags_direct_access_and_passes_clean_code(tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_text("import json\n\ndef fetch(host, request):\n    return host.http(request)\n")
    dirty = tmp_path / "dirty.py"
    dirty.write_text("import requests\nimport os\nTOKEN = os.environ['X']\n")
    driver = tmp_path / "driver.py"
    driver.write_text("import psycopg2\n")
    good = str(ROOT / "docs" / "capability-examples" / "mcp-server.json")
    assert run(good, "--source", str(clean))[0] == 0
    rc, out, _ = run(good, "--source", str(dirty))
    assert rc == 1 and "E-SRC-DIRECT-ACCESS" in out
    rc, out, _ = run(good, "--source", str(driver))
    assert rc == 0 and "W-SRC-DRIVER" in out
    assert run(good, "--source", str(driver), "--strict")[0] == 1


# ---------- network.owner_configured (P0-E) ----------

def test_owner_configured_satisfies_the_external_send_host_rule():
    d = mutate(lambda d: d.update(network={"owner_configured": True}))
    assert "E-NET-REQUIRED" not in codes(d) and levels(d, "error") == set()


def test_owner_configured_only_accepts_true():
    for bad in (False, "yes", 1):
        d = mutate(lambda d: d.update(network={"owner_configured": bad}))
        assert "E-SCHEMA" in levels(d, "error")


def test_owner_configured_does_not_excuse_an_internal_host():
    d = mutate(lambda d: d.update(network={"owner_configured": True, "hosts": ["10.0.0.5"]}))
    assert "E-NET-INTERNAL-HOST" in levels(d, "error")


def test_an_empty_network_still_needs_a_host_for_external_send():
    d = mutate(lambda d: d.update(network={}))
    assert "E-NET-REQUIRED" in levels(d, "error")


def test_a_constant_is_compared_by_type_so_1_is_not_true():
    for field in ("owner_configured", "https_only"):
        for bad in (1, 0, "true"):
            d = mutate(lambda d: d["network"].update({field: bad}))
            assert "E-SCHEMA" in levels(d, "error"), (field, bad)
        assert "E-SCHEMA" not in levels(mutate(lambda d: d["network"].update({field: True})), "error")
