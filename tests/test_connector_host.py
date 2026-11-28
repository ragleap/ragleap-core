"""Host-connector contract (Phase 0, P0-C). The contract tests are written once, against the interface, so that the
isolated host of a later phase can be run through exactly the same suite."""
import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import pytest

import connector_fixtures as fx
import ragleap_boundary_scan as scan
import ragleap_conformance as conf
from core.connector_host import (HOST_VERSION, ConnectorHost, Context, ContractError, HostError, HostServices,
                                 InProcessHost, Reply, Request, Usage)
from core.connector_host import contract as c

SCHEMA = json.loads((ROOT / "docs" / "capability-schema-v1.schema.json").read_text(encoding="utf-8"))


def req(op, args=None, rid="r1", **ctx):
    return Request(id=rid, operation=op, args=args or {}, context=Context(run_id="run-1", actor="support", **ctx))


class HostContract:
    """Behaviour every ConnectorHost must show. Subclass and implement make_host."""

    def make_host(self, declaration, connector, **kw) -> ConnectorHost:
        raise NotImplementedError

    def test_describe_returns_a_copy(self):
        host = self.make_host(fx.ECHO_DECL, fx.EchoConnector())
        d = host.describe()
        d["operations"].append({"name": "evil", "effect": "execute"})
        assert [o["name"] for o in host.describe()["operations"]] == ["echo"]

    def test_a_declared_operation_runs_and_the_request_id_comes_back(self):
        r = self.make_host(fx.ECHO_DECL, fx.EchoConnector()).invoke(req("echo", {"a": 1}, rid="abc"))
        assert (r.status, r.id, r.v, r.data) == ("ok", "abc", HOST_VERSION, {"echo": {"a": 1}})

    def test_an_undeclared_operation_is_denied_and_the_connector_is_not_called(self):
        conn = fx.EchoConnector()
        r = self.make_host(fx.ECHO_DECL, conn).invoke(req("delete_everything"))
        assert (r.status, r.error_code) == ("denied", c.E_UNDECLARED_OPERATION) and conn.calls == 0

    def test_a_different_contract_version_is_denied(self):
        conn = fx.EchoConnector()
        d = req("echo").to_dict()
        d["v"] = "ragleap.host/2"
        r = self.make_host(fx.ECHO_DECL, conn).invoke(d)
        assert (r.status, r.error_code) == ("denied", c.E_UNSUPPORTED_VERSION) and conn.calls == 0

    @pytest.mark.parametrize("bad", [
        {}, {"id": "x"}, {"id": "x", "operation": "echo"}, {"id": "x", "operation": "echo", "context": {"run_id": ""}},
        {"id": "x", "operation": "echo", "args": [], "context": {"run_id": "r"}},
        {"id": "x", "operation": "echo", "extra": 1, "context": {"run_id": "r"}},
        {"id": "x", "operation": "echo", "context": {"run_id": "r", "scope_level": "galaxy"}},
        {"id": "x", "operation": "echo", "context": {"run_id": "r", "deadline_ms": -1}},
        "not a dict",
    ])
    def test_a_malformed_request_gets_a_bad_request_reply_not_an_exception(self, bad):
        conn = fx.EchoConnector()
        r = self.make_host(fx.ECHO_DECL, conn).invoke(bad)
        assert (r.status, r.error_code) == ("error", c.E_BAD_REQUEST) and conn.calls == 0

    def test_a_request_dict_and_a_request_object_are_equivalent_and_the_reply_is_json(self):
        host = self.make_host(fx.ECHO_DECL, fx.EchoConnector())
        a = host.invoke(req("echo", {"k": "v"}))
        b = host.invoke(req("echo", {"k": "v"}).to_dict())
        assert a.data == b.data and a.status == b.status
        back = Reply.from_dict(json.loads(json.dumps(a.to_dict())))
        assert (back.status, back.data, back.id) == (a.status, a.data, a.id)

    def test_the_host_measures_calls_and_bytes_and_records_the_egress(self):
        t = fx.FakeTransport(size=10)
        r = self.make_host(fx.FETCH_DECL, fx.FetchConnector(), transport=t).invoke(req("fetch"))
        assert r.status == "ok" and len(t.calls) == 2
        assert (r.usage.calls, r.usage.bytes_in, r.usage.bytes_out) == (2, 20, 8)
        assert [(e.kind, e.host, e.method, e.status) for e in r.egress] == [("http", "api.example-fixture.com", "GET", 200)] * 2

    def test_only_the_measured_usage_counts_and_a_misreport_is_kept_apart(self):
        t = fx.FakeTransport(size=10)
        conn = fx.FetchConnector(report={"calls": 0, "bytes_in": 0, "bytes_out": 0})
        r = self.make_host(fx.FETCH_DECL, conn, transport=t).invoke(req("fetch"))
        assert r.usage.calls == 2 and r.usage.bytes_in == 20          # measured, whatever the connector said
        assert r.reported_usage.calls == 0 and r.reported_usage.bytes_in == 0

    def test_garbage_reported_usage_is_ignored(self):
        conn = fx.FetchConnector(report={"calls": "many", "bytes_in": -5, "bytes_out": True, "ms": 1.5})
        r = self.make_host(fx.FETCH_DECL, conn, transport=fx.FakeTransport()).invoke(req("fetch"))
        assert r.reported_usage == Usage()

    def test_http_to_an_undeclared_host_is_refused_and_nothing_is_sent(self):
        t = fx.FakeTransport()
        r = self.make_host(fx.FETCH_DECL, fx.FetchConnector(url="https://evil.example.org/x"), transport=t).invoke(req("fetch"))
        assert (r.status, r.error_code) == ("error", c.E_HOST_NOT_DECLARED) and t.calls == []

    @pytest.mark.parametrize("url", ["http://api.example-fixture.com/data", "ftp://api.example-fixture.com/data"])
    def test_only_https_is_allowed(self, url):
        t = fx.FakeTransport()
        r = self.make_host(fx.FETCH_DECL, fx.FetchConnector(url=url), transport=t).invoke(req("fetch"))
        assert r.error_code == c.E_INSECURE_URL and t.calls == []

    def test_a_subdomain_is_not_covered_by_the_declared_host(self):
        t = fx.FakeTransport()
        r = self.make_host(fx.FETCH_DECL, fx.FetchConnector(url="https://x.api.example-fixture.com/"), transport=t).invoke(req("fetch"))
        assert r.error_code == c.E_HOST_NOT_DECLARED

    def test_without_a_transport_the_host_has_no_network(self):
        r = self.make_host(fx.FETCH_DECL, fx.FetchConnector()).invoke(req("fetch"))
        assert (r.status, r.error_code) == ("error", c.E_NO_TRANSPORT)

    def test_a_declared_secret_is_available_and_never_appears_in_the_reply(self):
        t = fx.FakeTransport()
        conn = fx.FetchConnector(use_secret=True)
        r = self.make_host(fx.FETCH_DECL, conn, transport=t, secrets=lambda n: "tok-SECRET-9" if n == "FIXTURE_TOKEN" else None).invoke(req("fetch"))
        assert r.status == "ok" and "tok-SECRET-9" not in json.dumps(r.to_dict())

    def test_an_undeclared_secret_is_refused_and_a_missing_value_is_reported(self):
        class Wants:
            def __init__(self, name):
                self.name = name

            def invoke(self, request, services):
                services.secret(self.name)
                return {"status": "ok", "data": {}}
        r = self.make_host(fx.FETCH_DECL, Wants("OTHER_TOKEN"), secrets=lambda n: "v").invoke(req("fetch"))
        assert r.error_code == c.E_SECRET_NOT_DECLARED
        r = self.make_host(fx.FETCH_DECL, Wants("FIXTURE_TOKEN"), secrets=lambda n: None).invoke(req("fetch"))
        assert r.error_code == c.E_SECRET_UNAVAILABLE

    def test_tcp_is_reserved_and_answers_unsupported(self):
        r = self.make_host(fx.TCP_DECL, fx.TcpConnector()).invoke(req("query"))
        assert (r.status, r.error_code) == ("error", c.E_UNSUPPORTED) and r.usage.calls == 0

    def test_a_crashing_connector_never_escapes_and_its_message_is_not_copied(self):
        r = self.make_host(fx.ECHO_DECL, fx.CrashConnector()).invoke(req("echo"))
        assert (r.status, r.error_code, r.error_message) == ("error", c.E_CONNECTOR_ERROR, "RuntimeError")
        assert "secret-value-12345" not in json.dumps(r.to_dict())

    def test_a_reply_that_is_too_large_is_refused(self):
        big = fx.RawConnector({"status": "ok", "data": "x" * (c.MAX_REPLY_BYTES + 10)})
        r = self.make_host(fx.ECHO_DECL, big).invoke(req("echo"))
        assert (r.status, r.error_code, r.data) == ("error", c.E_REPLY_TOO_LARGE, None)

    @pytest.mark.parametrize("out", [None, "text", {"status": "weird"}, {"status": "ok", "data": object()}, {"data": 1}])
    def test_an_invalid_connector_reply_becomes_a_connector_error(self, out):
        r = self.make_host(fx.ECHO_DECL, fx.RawConnector(out)).invoke(req("echo"))
        assert (r.status, r.error_code) == ("error", c.E_CONNECTOR_ERROR)

    def test_an_unknown_error_code_from_a_connector_is_mapped_to_connector_error(self):
        r = self.make_host(fx.ECHO_DECL, fx.RawConnector({"status": "error", "error_code": "made_up", "error_message": "x"})).invoke(req("echo"))
        assert r.error_code == c.E_CONNECTOR_ERROR

    def test_a_write_operation_runs_through_the_same_path(self):
        conn = fx.NoteConnector()
        r = self.make_host(fx.NOTE_DECL, conn).invoke(req("append_note", {"text": "hi"}))
        assert r.status == "ok" and conn.notes == ["hi"]


class TestInProcessHostContract(HostContract):
    def make_host(self, declaration, connector, **kw):
        return InProcessHost(declaration, connector, **kw)


# ---------- things specific to this phase ----------

@pytest.mark.parametrize("decl", fx.ALL_DECLS, ids=lambda d: d["id"])
def test_the_fixture_declarations_conform_to_the_published_schema(decl):
    assert conf.validate(decl, SCHEMA) == []
    assert not [f for f in conf.semantic_findings(decl) if f[0] == "error"]


def test_the_interfaces_are_abstract_and_the_messages_are_frozen():
    with pytest.raises(TypeError):
        HostServices()
    with pytest.raises(TypeError):
        ConnectorHost()
    r = req("echo")
    with pytest.raises(Exception):
        r.operation = "other"
    with pytest.raises(Exception):
        r.context.run_id = "x"


def test_hosterror_keeps_a_known_code_and_maps_an_unknown_one():
    assert HostError(c.E_UNSUPPORTED).code == c.E_UNSUPPORTED
    assert HostError("nonsense").code == c.E_CONNECTOR_ERROR


# P1-F (deliberate change): the start record hashes each tool's DECLARED effect class, so the gate module reads the constants table
# BUILTIN_EFFECT from the host's policy module. It is the one allowed reference, it must be a lazy import inside tools_digest, and
# nothing else of the host may be imported until Phase 1 proper wires the gate to the host on purpose.
ALLOWED_HOST_REFERENCES = {"core/autonomy.py": ("from core.connector_host.policy import BUILTIN_EFFECT",)}


def test_nothing_in_the_product_imports_the_connector_host_yet():
    """Phase 0 changes no behaviour. Phase 1 will update this test on purpose when the gate starts to use the host."""
    offenders = []
    for base in ("core", "channels", "sandbox"):
        for p in (ROOT / base).rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith("core/connector_host/"):
                continue
            text = p.read_text(encoding="utf-8")
            for allowed in ALLOWED_HOST_REFERENCES.get(rel, ()):
                assert text.count(allowed) == 1, (rel, allowed)
                text = text.replace(allowed, "")
            if "connector_host" in text:
                offenders.append(rel)
    assert offenders == []


def test_the_one_allowed_host_reference_is_a_lazy_import_inside_tools_digest():
    import ast
    tree = ast.parse((ROOT / "core" / "autonomy.py").read_text(encoding="utf-8"))
    for node in tree.body:                                   # nothing at module level
        assert not (isinstance(node, (ast.Import, ast.ImportFrom)) and "connector_host" in ast.dump(node))
    holders = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and any(isinstance(s, ast.ImportFrom) and (s.module or "").startswith("core.connector_host") for s in ast.walk(n))]
    assert holders == ["tools_digest"]


def test_the_host_package_itself_uses_no_network_environment_or_subprocess():
    for p in sorted((ROOT / "core" / "connector_host").glob("*.py")):
        violations, _ = scan.scan_connector(p.read_text(encoding="utf-8"))
        assert violations == [], f"{p.name}: {violations}"
