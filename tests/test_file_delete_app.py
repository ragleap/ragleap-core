"""App side of deleting a file (tool delete_file): the two switches, plan checks, the file check bound to the action,
approval always (even in full mode), the human display, what is logged, and the exact call to the runner.
The runner and the database are replaced; nothing here touches a disk, Docker or the network."""
import datetime
import json
import pathlib

import pytest

from core import agent_loop, autonomy, file_access
from core.employees import actions

ROOT = pathlib.Path(__file__).resolve().parent.parent
A = "a" * 64
STAMP = "20261113T010101Z-ab12cd"


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


@pytest.fixture(autouse=True)
def _fresh_cache():
    file_access._cache.update({"at": 0.0, "names": [], "modes": {}})
    yield
    file_access._cache.update({"at": 0.0, "names": [], "modes": {}})


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("FILE_TOOLS_ENABLED", "true")
    monkeypatch.setenv("FILE_WRITE_ENABLED", "true")
    monkeypatch.setenv("FILE_DELETE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_TOKEN", "t")
    grants = {"grants": [{"name": "notes", "mode": "write", "purpose": "use", "expires": None},
                         {"name": "ro", "mode": "read", "purpose": "use", "expires": None}]}
    monkeypatch.setattr(file_access.requests, "get", lambda *a, **k: Resp(200, grants))


def stat_ok(**kw):
    d = {"op": "stat", "exists": True, "size": 12, "sha256": A, "binary": False, "head": "old notes\nline two\n"}
    d.update(kw)
    return {"ok": True, "result": d}


def runner(monkeypatch, replies):
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        seen.append(json)
        code, body = replies[json["op"]]
        return Resp(code, body)
    monkeypatch.setattr(file_access.requests, "post", fake_post)
    return seen


# ---------------- the tool and its plan checks ----------------

def test_the_tool_needs_both_switches_and_a_write_grant(on, monkeypatch):
    assert "delete_file" in actions.available_tools()
    monkeypatch.delenv("FILE_DELETE_ENABLED")
    tools = actions.available_tools()
    assert "delete_file" not in tools and "write_file" in tools
    monkeypatch.setenv("FILE_DELETE_ENABLED", "true")
    monkeypatch.delenv("FILE_WRITE_ENABLED")
    tools = actions.available_tools()
    assert "delete_file" not in tools and "write_file" not in tools
    monkeypatch.setenv("FILE_WRITE_ENABLED", "true")
    file_access._cache.update({"at": 0.0})
    only_read = {"grants": [{"name": "ro", "mode": "read", "purpose": "use", "expires": None}]}
    monkeypatch.setattr(file_access.requests, "get", lambda *a, **k: Resp(200, only_read))
    assert "delete_file" not in actions.available_tools()


def plan(content, target="notes"):
    return actions._validate_plan({"tool": "delete_file", "target": target, "content": content}, actions.available_tools())


def test_a_good_plan_and_the_bad_ones(on, monkeypatch):
    p = plan("  old/notes.txt \n")
    assert p["channel"] == "files_delete" and p["target"] == "notes" and p["content"] == "old/notes.txt"
    assert plan("a.txt", target="ro") is None and plan("a.txt", target="nope") is None
    assert plan("../x") is None and plan("/etc/passwd") is None and plan("a\\b") is None
    assert plan("") is None and plan("a.txt\nand some text") is None and plan("x" * 400) is None
    monkeypatch.delenv("FILE_DELETE_ENABLED")
    assert plan("a.txt") is None


def test_it_is_outbound_style_and_not_external_content():
    assert "delete_file" in agent_loop.OUTBOUND_TOOLS and "delete_file" not in agent_loop.TAINTING_TOOLS


# ---------------- the file check bound to the action ----------------

def test_stat_raw_returns_the_dict_and_sends_only_op_grant_path(on, monkeypatch):
    seen = runner(monkeypatch, {"stat": (200, stat_ok())})
    d, why = file_access.stat_raw("notes", "old.txt")
    assert why is None and d["sha256"] == A
    assert seen == [{"op": "stat", "grant": "notes", "path": "old.txt"}]


@pytest.mark.parametrize("grant,rel", [("ro", "a.txt"), ("nope", "a.txt"), ("notes", "../a"), ("notes", "")])
def test_stat_raw_refuses_bad_input_without_a_call(on, monkeypatch, grant, rel):
    seen = runner(monkeypatch, {})
    d, why = file_access.stat_raw(grant, rel)
    assert d is None and why and seen == []


def test_stat_raw_refuses_a_missing_file_an_incomplete_check_and_runner_errors(on, monkeypatch):
    runner(monkeypatch, {"stat": (200, {"ok": True, "result": {"op": "stat", "exists": False}})})
    assert file_access.stat_raw("notes", "a.txt") == (None, "that file does not exist")
    runner(monkeypatch, {"stat": (200, stat_ok(sha256="short"))})
    assert file_access.stat_raw("notes", "a.txt")[0] is None
    runner(monkeypatch, {"stat": (200, {"ok": False, "error": "a symbolic link is in that path"})})
    assert file_access.stat_raw("notes", "a.txt") == (None, "a symbolic link is in that path")
    monkeypatch.delenv("FILE_DELETE_ENABLED")
    assert file_access.stat_raw("notes", "a.txt") == (None, "deleting files is not enabled")


class _Cur:
    inserts = []
    rows = []

    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("INSERT"):
            _Cur.inserts.append(params)

    def fetchall(self):
        return _Cur.rows

    def close(self):
        pass


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def gate(monkeypatch):
    _Cur.inserts, _Cur.rows = [], []
    state = {"mode": "full"}
    monkeypatch.setattr(autonomy, "get_autonomy_settings", lambda: {
        "mode": state["mode"], "actions": [], "channels": [], "approval_channel": "", "approval_target": ""})
    monkeypatch.setattr(autonomy, "get_connection", lambda: _Conn())
    monkeypatch.setattr(autonomy, "request_approval", lambda *a, **k: None)

    def boom(*a, **k):
        raise AssertionError("a delete must not run without approval")
    monkeypatch.setattr(autonomy, "_send_via_channel", boom)
    return state


def propose(content="old.txt"):
    return autonomy.execute_or_request(action_type="delete_file", channel="files_delete", target="notes", content=content)


@pytest.mark.parametrize("mode", ["full", "semi"])
def test_a_delete_always_waits_for_approval_and_stores_the_bound_check(on, gate, monkeypatch, mode):
    gate["mode"] = mode
    runner(monkeypatch, {"stat": (200, stat_ok())})
    assert propose()["status"] == "pending_approval"
    stored = json.loads(_Cur.inserts[0][4])
    assert stored == {"path": "old.txt", "sha": A, "size": 12, "binary": False, "head": "old notes\nline two\n"}


def test_a_binary_file_stores_no_head(on, gate, monkeypatch):
    runner(monkeypatch, {"stat": (200, stat_ok(binary=True, head="should not be kept"))})
    propose()
    assert json.loads(_Cur.inserts[0][4])["head"] == ""


def test_a_missing_or_refused_file_creates_no_pending_action(on, gate, monkeypatch):
    runner(monkeypatch, {"stat": (200, {"ok": True, "result": {"op": "stat", "exists": False}})})
    res = propose()
    assert res["status"] == "skipped" and "does not exist" in res["result"] and _Cur.inserts == []
    runner(monkeypatch, {"stat": (403, {"error": "this grant is read-only"})})
    res = propose()
    assert res["status"] == "skipped" and "read-only" in res["result"] and _Cur.inserts == []


def test_autonomy_off_asks_the_runner_nothing(on, gate, monkeypatch):
    gate["mode"] = "off"
    seen = runner(monkeypatch, {})
    assert propose()["status"] == "skipped" and seen == []


# ---------------- what people read, what is logged ----------------

BOUND = json.dumps({"path": "old.txt", "sha": A, "size": 12, "binary": False, "head": "SECRET-OLD-TEXT\nline two\n"})


def test_the_approval_text_shows_path_size_hash_and_the_start():
    s = autonomy._approval_preview("files_delete", BOUND)
    assert "old.txt" in s and "trash" in s and "12 bytes" in s and A[:16] in s and "SECRET-OLD-TEXT" in s
    b = json.dumps(dict(json.loads(BOUND), binary=True, head=""))
    assert "binary" in autonomy._approval_preview("files_delete", b)
    assert autonomy._approval_preview("files_delete", "not json") == "not json"
    assert autonomy._approval_preview("slack", "x" * 500) == "x" * 300


def test_the_pending_list_carries_a_display_field(monkeypatch):
    _Cur.inserts, _Cur.rows = [], [("ABCD1234", "delete_file", "files_delete", "notes", BOUND, "", None,
                                    datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc))]
    monkeypatch.setattr(autonomy, "get_connection", lambda: _Conn())
    got = autonomy.list_pending()
    assert "Delete file: old.txt" in got[0]["display"] and got[0]["content"] == BOUND


def test_the_log_and_learning_keep_no_file_content():
    s = autonomy._log_content("files_delete", BOUND)
    assert "SECRET-OLD-TEXT" not in s and "old.txt" in s and A[:16] in s
    assert autonomy._log_content("files_delete", "not json") == "not json"
    assert autonomy._log_content("slack", "hello") == "hello"


# ---------------- the delete itself ----------------

def test_dispatch_and_the_exact_call_to_the_runner(on, monkeypatch):
    body = {"ok": True, "result": {"op": "delete", "deleted": True, "old_sha256": A, "size": 12,
                                   "trash": ".ragleap-trash/%s/old.txt" % STAMP}}
    seen = runner(monkeypatch, {"delete": (200, body)})
    emitted = []
    from core import audit
    monkeypatch.setattr(audit, "emit", lambda ev, **f: emitted.append((ev, f)))
    out = autonomy._send_via_channel("files_delete", "notes", BOUND)
    assert seen == [{"op": "delete", "grant": "notes", "path": "old.txt", "expect_sha256": A}]
    assert out.startswith("File moved to the trash (not erased).") and STAMP in out
    assert "ragleap files trash restore notes " + STAMP in out and "SECRET-OLD-TEXT" not in out
    assert emitted[0][0] == "file_delete" and emitted[0][1]["old_sha256"] == A and emitted[0][1]["trash"].startswith(".ragleap-trash/")
    assert "head" not in emitted[0][1]


@pytest.mark.parametrize("reply,want", [
    ((403, {"error": "writing is off"}), "writing is off"),
    ((403, {"error": "this grant is read-only"}), "read-only"),
    ((200, {"ok": False, "error": "the file changed since the preview"}), "changed since the preview"),
    ((200, {"ok": False, "error": "the trash is full; the owner must empty it"}), "trash is full"),
    ((429, {}), "busy"),
])
def test_runner_refusals_are_plain_and_say_nothing_was_moved(on, monkeypatch, reply, want):
    runner(monkeypatch, {"delete": reply})
    out = file_access.delete_file("notes", BOUND)
    assert out.startswith("File delete: nothing was moved") and want in out


def test_an_invalid_approved_action_is_refused_without_a_call(on, monkeypatch):
    seen = runner(monkeypatch, {})
    good = json.loads(BOUND)
    for b in (json.dumps(dict(good, sha="zz")), json.dumps(dict(good, path="../x")), json.dumps(dict(good, path=5)), "nope", "[]"):
        assert "refused (the approved action is not valid)" in file_access.delete_file("notes", b)
    assert "refused" in file_access.delete_file("ro", BOUND) and "refused" in file_access.delete_file("nope", BOUND)
    assert seen == []


def test_with_either_switch_off_an_approved_delete_is_refused(on, monkeypatch):
    seen = runner(monkeypatch, {})
    monkeypatch.delenv("FILE_DELETE_ENABLED")
    assert "not enabled" in file_access.delete_file("notes", BOUND)
    monkeypatch.setenv("FILE_DELETE_ENABLED", "true")
    monkeypatch.delenv("FILE_WRITE_ENABLED")
    assert "not enabled" in file_access.delete_file("notes", BOUND) and seen == []


def test_a_runner_crash_never_raises(on, monkeypatch):
    def crash(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(file_access.requests, "post", crash)
    assert "nothing was moved" in file_access.delete_file("notes", BOUND)


def test_the_docs_describe_the_delete_tool_and_its_switch():
    text = (ROOT / "docs" / "file-access.md").read_text()
    assert "FILE_DELETE_ENABLED" in text and "delete_file" in text and "{{" not in text and "{%" not in text
    assert "Nothing gives the agent a delete tool yet" not in text
