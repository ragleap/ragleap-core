"""GET /audit filters (core/audit.py list_events): method, path prefix, status, since/until and paging by id.
Real database (db/schema.sql applied); test rows are removed afterwards."""
import pytest
from fastapi.testclient import TestClient

from core import audit, document_store, users

BOOT = {"x-api-key": "boot-key"}
MARK = "pytest-auditf-"


def _sql(sql, args=(), fetch=False):
    conn = document_store._connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def clean():
    users._clear_cache()
    first = _sql("SELECT COALESCE(MAX(id), 0) FROM audit_events", fetch=True)[0][0]
    yield
    _sql("DELETE FROM audit_events WHERE id > %s", (first,))
    users._clear_cache()


@pytest.fixture
def client(monkeypatch):
    from core import api
    monkeypatch.setattr(api, "RAGLEAP_API_KEY", "boot-key")
    return TestClient(api.app)


def _rec(who, method, path, status=200):
    audit.record({"name": MARK + who, "role": "admin", "kind": "user"}, method, path, status)


def _paths(**kw):
    return sorted(e["path"] for e in audit.list_events(limit=50, actor=MARK + "a", **kw))


def test_filters_by_method_path_prefix_and_status():
    _rec("a", "POST", "/users")
    _rec("a", "DELETE", "/users/x")
    _rec("a", "POST", "/documents", 403)
    assert _paths(method="delete") == ["/users/x"]
    assert _paths(path="/users") == ["/users", "/users/x"]
    assert _paths(path="/doc") == ["/documents"]
    assert _paths(status=403) == ["/documents"]
    assert _paths(method="POST", path="/users") == ["/users"]
    assert _paths(method="PUT") == []


def test_the_path_filter_takes_percent_and_underscore_literally():
    _rec("a", "POST", "/a_b")
    _rec("a", "POST", "/axb")
    assert _paths(path="/a_b") == ["/a_b"]
    assert _paths(path="/a%") == []


def test_since_and_until():
    for p in ("/one", "/two", "/three"):
        _rec("a", "POST", p)
    for p, day in (("/one", "01"), ("/two", "02"), ("/three", "03")):
        _sql("UPDATE audit_events SET at = %s WHERE path = %s AND actor_name = %s",
             ("2026-01-%sT12:00:00+00:00" % day, p, MARK + "a"))
    assert _paths(since="2026-01-02T00:00:00Z") == ["/three", "/two"]
    assert _paths(until="2026-01-03T00:00:00Z") == ["/one", "/two"]
    assert _paths(since="2026-01-02T00:00:00Z", until="2026-01-03T00:00:00Z") == ["/two"]
    assert _paths(since="2026-01-02") == ["/three", "/two"]


def test_before_id_reads_the_next_page():
    for i in range(5):
        _rec("a", "POST", "/p%d" % i)
    p1 = audit.list_events(limit=2, actor=MARK + "a")
    p2 = audit.list_events(limit=2, actor=MARK + "a", before_id=p1[-1]["id"])
    p3 = audit.list_events(limit=2, actor=MARK + "a", before_id=p2[-1]["id"])
    assert [e["path"] for e in p1 + p2 + p3] == ["/p4", "/p3", "/p2", "/p1", "/p0"]


def test_a_bad_time_is_a_clear_error():
    with pytest.raises(ValueError, match="ISO 8601"):
        audit.list_events(since="yesterday")


def test_the_route_takes_the_filters_and_refuses_a_bad_time(client):
    _rec("a", "DELETE", "/users/x")
    got = client.get("/audit?method=delete&limit=5", headers=BOOT)
    assert got.status_code == 200 and got.json()["events"]
    assert all(e["method"] == "DELETE" for e in got.json()["events"])
    bad = client.get("/audit?since=nonsense", headers=BOOT)
    assert bad.status_code == 400 and "ISO 8601" in bad.json()["detail"]
