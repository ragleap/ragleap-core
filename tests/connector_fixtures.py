"""Fixture connectors for the host contract tests. Used only by tests: nothing in the product imports this module."""
from core.connector_host.services import HttpResult

BASE = {"schema": "ragleap.capability/1", "version": "1.0.0", "kind": "action"}

ECHO_DECL = dict(BASE, id="fixture.echo", operations=[{"name": "echo", "effect": "read"}])
NOTE_DECL = dict(BASE, id="fixture.notes", operations=[{"name": "append_note", "effect": "write", "approval": "always"}])
FETCH_DECL = dict(BASE, id="fixture.fetch", kind="datasource",
                  operations=[{"name": "fetch", "effect": "read", "taint": "tainting"}],
                  network={"hosts": ["api.example-fixture.com"], "https_only": True},
                  secrets=["FIXTURE_TOKEN"])
TCP_DECL = dict(BASE, id="fixture.tcp", kind="datasource", operations=[{"name": "query", "effect": "read"}],
                network={"tcp": [{"host": "db.example-fixture.com", "port": 5432}]})
ALL_DECLS = [ECHO_DECL, NOTE_DECL, FETCH_DECL, TCP_DECL]


class EchoConnector:
    def __init__(self):
        self.calls = 0

    def invoke(self, request, services):
        self.calls += 1
        return {"status": "ok", "data": {"echo": request.args}}


class NoteConnector:
    def __init__(self):
        self.notes = []

    def invoke(self, request, services):
        self.notes.append(request.args.get("text", ""))
        services.log({"event": "note_added", "count": len(self.notes)})
        return {"status": "ok", "data": {"count": len(self.notes)}}


class FetchConnector:
    """Reads two pages through the host and reports its own (honest or not) usage."""

    def __init__(self, url="https://api.example-fixture.com/data", use_secret=False, report=None):
        self.url, self.use_secret, self.report = url, use_secret, report

    def invoke(self, request, services):
        headers = {}
        if self.use_secret:
            headers["Authorization"] = "Bearer " + services.secret("FIXTURE_TOKEN")
        sizes = []
        for _ in range(2):
            r = services.http("GET", self.url, headers=headers, body=b"ping")
            sizes.append(len(r.body))
        out = {"status": "ok", "data": {"sizes": sizes}}
        if self.report is not None:
            out["usage"] = self.report
        return out


class TcpConnector:
    def invoke(self, request, services):
        services.tcp("db.example-fixture.com", 5432, "query")
        return {"status": "ok", "data": {}}


class CrashConnector:
    def invoke(self, request, services):
        raise RuntimeError("secret-value-12345 leaked in an exception message")


class RawConnector:
    """Returns whatever it was given: used to test how the host handles bad replies."""

    def __init__(self, out):
        self.out = out

    def invoke(self, request, services):
        return self.out


class FakeTransport:
    def __init__(self, size=10):
        self.calls, self.size = [], size

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        return HttpResult(200, {}, b"x" * self.size)
