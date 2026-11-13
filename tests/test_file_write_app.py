"""App side of writing a file (tool write_file): the kill switch, plan checks, the preview bound to the action,
approval always (even in full mode), the human display, what is logged, and the exact call to the runner.
The runner and the database are replaced; nothing here touches a disk, Docker or the network."""
import datetime
import json
import pathlib

import pytest

from core import agent_loop, autonomy, file_access
from core.employees import actions

ROOT = pathlib.Path(__file__).resolve().parent.parent
A, B, C = "a" * 64, "b" * 64, "c" * 64


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
    monkeypatch.setenv("SANDBOX_TOKEN", "t")
    grants = {"grants": [{"name": "notes", "mode": "write", "purpose": "use", "expires": None},
                         {"name": "ro", "mode": "read", "purpose": "use", "expires": None}]}
    monkeypatch.setattr(file_access.requests, "get", lambda *a, **k: Resp(200, grants))


def preview_ok(**kw):
    d = {"op": "preview", "exists": True, "old_size": 6, "new_size": 7, "old_sha256": A, "new_sha256": B,
         "identical": False, "added": 1, "removed": 1, "diff": "--- a/todo.txt\n+++ b/todo.txt\n-old\n+new!\n"}
    d.update(kw)
    return {"ok": True, "result": d}


def runner(monkeypatch, replies):
    """Fake runner: replies maps an op to (status, body). Records every call."""
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        seen.append(json)
        code, body = replies[json["op"]]
        return Resp(code, body)
    monkeypatch.setattr(file_access.requests, "post", fake_post)
    return seen


# ---------------- the tool and its plan checks ----------------

def test_the_tool_needs_the_kill_switch_and_a_write_grant(on, monkeypatch):
    assert "write_file" in actions.available_tools()
    monkeypatch.delenv("FILE_WRITE_ENABLED")
    assert "write_file" not in actions.available_tools() and "preview_change" in actions.available_tools()
    monkeypatch.setenv("FILE_WRITE_ENABLED", "true")
    file_access._cache.update({"at": 0.0})
    only_read = {"grants": [{"name": "ro", "mode": "read", "purpose": "use", "expires": None}]}
    monkeypatch.setattr(file_access.requests, "get", lambda *a, **k: Resp(200, only_read))
    assert "write_file" not in actions.available_tools()


def plan(content, target="notes"):
    return actions._validate_plan({"tool": "write_file", "target": target, "content": content}, actions.available_tools())


def test_a_good_plan_and_the_bad_ones(on, monkeypatch):
    p = plan("todo.txt\nnew text\n")
    assert p["channel"] == "files_write" and p["target"] == "notes" and p["content"] == "todo.txt\nnew text"
    assert plan("todo.txt\nx", target="ro") is None and plan("todo.txt\nx", target="nope") is None
    assert plan("../x\ntext") is None and plan("/etc/passwd\ntext") is None and plan("a\\b\ntext") is None
    assert plan("todo.txt\n" + "x" * 2000) is None  # never silently cut
    assert plan("todo.txt") is None and plan("todo.txt\n   \n") is None  # no empty file by accident
    monkeypatch.delenv("FILE_WRITE_ENABLED")
    assert plan("todo.txt\nx") is None


def test_it_is_outbound_style_and_not_external_content():
    assert "write_file" in agent_loop.OUTBOUND_TOOLS and "write_file" not in agent_loop.TAINTING_TOOLS


# ---------------- the preview bound to the action ----------------

def test_preview_raw_returns_the_dict_and_sends_only_op_grant_path_text(on, monkeypatch):
    seen = runner(monkeypatch, {"preview": (200, preview_ok())})
    d, why = file_access.preview_raw("notes", "todo.txt", "new!")
    assert why is None and d["new_sha256"] == B
    assert seen == [{"op": "preview", "grant": "notes", "path": "todo.txt", "text": "new!"}]


@pytest.mark.parametrize("grant,rel,text", [("ro", "a.txt", "x"), ("nope", "a.txt", "x"), ("notes", "../a", "x"),
                                            ("notes", "a.txt", "  ")])
def test_preview_raw_refuses_bad_input_without_a_call(on, monkeypatch, grant, rel, text):
    seen = runner(monkeypatch, {})
    d, why = file_access.preview_raw(grant, rel, text)
    assert d is None and why and seen == []


def test_preview_raw_refuses_an_incomplete_preview_and_runner_errors(on, monkeypatch):
    runner(monkeypatch, {"preview": (200, preview_ok(new_sha256="short"))})
    assert file_access.preview_raw("notes", "a.txt", "x")[0] is None
    runner(monkeypatch, {"preview": (200, {"ok": False, "error": "that is a binary file"})})
    assert file_access.preview_raw("notes", "a.txt", "x") == (None, "that is a binary file")


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
        raise AssertionError("a write must not run without approval")
    monkeypatch.setattr(autonomy, "_send_via_channel", boom)
    return state


def propose(content="todo.txt\nnew!"):
    return autonomy.execute_or_request(action_type="write_file", channel="files_write", target="notes", content=content)


@pytest.mark.parametrize("mode", ["full", "semi"])
def test_a_write_always_waits_for_approval_and_stores_the_bound_preview(on, gate, monkeypatch, mode):
    gate["mode"] = mode
    runner(monkeypatch, {"preview": (200, preview_ok())})
    res = propose()
    assert res["status"] == "pending_approval"
    stored = json.loads(_Cur.inserts[0][4])
    assert stored["path"] == "todo.txt" and stored["base"] == A and stored["new"] == B and stored["text"] == "new!"
    assert "+new!" in stored["diff"] and stored["added"] == 1 and stored["removed"] == 1


def test_a_new_file_has_base_dash(on, gate, monkeypatch):
    runner(monkeypatch, {"preview": (200, preview_ok(exists=False, old_sha256=None, old_size=None))})
    propose()
    assert json.loads(_Cur.inserts[0][4])["base"] == "-"


def test_identical_or_refused_previews_create_no_pending_action(on, gate, monkeypatch):
    runner(monkeypatch, {"preview": (200, preview_ok(identical=True, new_sha256=A))})
    res = propose()
    assert res["status"] == "skipped" and "identical" in res["result"] and _Cur.inserts == []
    runner(monkeypatch, {"preview": (403, {"error": "this grant is read-only"})})
    res = propose()
    assert res["status"] == "skipped" and "read-only" in res["result"] and _Cur.inserts == []


def test_autonomy_off_asks_the_runner_nothing(on, gate, monkeypatch):
    gate["mode"] = "off"
    seen = runner(monkeypatch, {})
    assert propose()["status"] == "skipped" and seen == []


# ---------------- what people read, what is logged ----------------

BOUND = json.dumps({"path": "todo.txt", "base": A, "new": B, "text": "SECRET-NEW-TEXT", "exists": True,
                    "added": 2, "removed": 1, "diff": "-old\n+new!\n" + "+x\n" * 600})


def test_the_approval_text_shows_path_counts_hashes_and_a_short_diff():
    s = autonomy._approval_preview("files_write", BOUND)
    assert "todo.txt" in s and "replaces an existing file" in s and "2 lines added, 1 removed" in s
    assert A[:16] in s and B[:16] in s and "+new!" in s and len(s) < 1200
    new = json.dumps(dict(json.loads(BOUND), base="-"))
    assert "creates a new file" in autonomy._approval_preview("files_write", new)


def test_other_channels_keep_the_old_300_character_preview():
    assert autonomy._approval_preview("slack", "x" * 500) == "x" * 300
    assert autonomy._approval_preview("files_write", "not json") == "not json"


def test_the_pending_list_carries_a_display_field(monkeypatch):
    _Cur.inserts, _Cur.rows = [], [("ABCD1234", "write_file", "files_write", "notes", BOUND, "", None,
                                    datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc)),
                                   ("EFGH5678", "send_slack", "slack", "x", "hello", "", None, None)]
    monkeypatch.setattr(autonomy, "get_connection", lambda: _Conn())
    got = autonomy.list_pending()
    assert "2 lines added" in got[0]["display"] and got[0]["content"] == BOUND
    assert got[1]["display"] == "hello"


def test_the_log_and_learning_keep_no_file_text():
    s = autonomy._log_content("files_write", BOUND)
    assert "SECRET-NEW-TEXT" not in s and "todo.txt" in s and A[:16] in s
    assert autonomy._log_content("slack", "hello") == "hello"
    assert autonomy._log_content("files_write", "not json") == "not json"


def test_the_dashboard_prefers_the_display_field():
    assert 'p.display || p.content' in (ROOT / "core" / "office_static" / "app.js").read_text()


# ---------------- the write itself ----------------

def test_dispatch_and_the_exact_call_to_the_runner(on, monkeypatch):
    body = {"ok": True, "result": {"op": "write", "changed": True, "created": False, "old_sha256": A, "new_sha256": B,
                                   "bytes": 4, "trash": ".ragleap-trash/20261113T0101-ab/todo.txt"}}
    seen = runner(monkeypatch, {"write": (200, body)})
    emitted = []
    from core import audit
    monkeypatch.setattr(audit, "emit", lambda ev, **f: emitted.append((ev, f)))
    out = autonomy._send_via_channel("files_write", "notes", BOUND)
    assert seen == [{"op": "write", "grant": "notes", "path": "todo.txt", "text": "SECRET-NEW-TEXT",
                     "expect_sha256": A, "new_sha256": B}]
    assert out.startswith("File written: file replaced") and ".ragleap-trash" in out and "SECRET-NEW-TEXT" not in out
    assert emitted[0][0] == "file_write" and emitted[0][1]["old_sha256"] == A and "text" not in emitted[0][1]


def test_a_new_file_is_reported_as_created(on, monkeypatch):
    body = {"ok": True, "result": {"op": "write", "changed": True, "created": True, "old_sha256": None, "new_sha256": B}}
    runner(monkeypatch, {"write": (200, body)})
    new = json.dumps(dict(json.loads(BOUND), base="-"))
    assert "new file created" in file_access.write_file("notes", new)


@pytest.mark.parametrize("reply,want", [
    ((403, {"error": "writing is off"}), "writing is off"),
    ((403, {"error": "this grant is read-only"}), "read-only"),
    ((200, {"ok": False, "error": "the file changed since the preview"}), "changed since the preview"),
    ((200, {"ok": False, "error": "the trash is full"}), "trash is full"),
    ((429, {}), "busy"),
])
def test_runner_refusals_are_plain_and_say_nothing_was_written(on, monkeypatch, reply, want):
    runner(monkeypatch, {"write": reply})
    out = file_access.write_file("notes", BOUND)
    assert out.startswith("File write: nothing was written") and want in out


def test_an_invalid_approved_action_is_refused_without_a_call(on, monkeypatch):
    seen = runner(monkeypatch, {})
    bad = [json.dumps(dict(json.loads(BOUND), base="zz")), json.dumps(dict(json.loads(BOUND), new="1")),
           json.dumps(dict(json.loads(BOUND), path="../x")), json.dumps(dict(json.loads(BOUND), text=5)), "nope", "[]"]
    for b in bad:
        assert "refused (the approved action is not valid)" in file_access.write_file("notes", b)
    assert "refused" in file_access.write_file("ro", BOUND) and "refused" in file_access.write_file("nope", BOUND)
    assert seen == []


def test_with_the_kill_switch_off_an_approved_write_is_refused(on, monkeypatch):
    seen = runner(monkeypatch, {})
    monkeypatch.delenv("FILE_WRITE_ENABLED")
    assert "not enabled" in file_access.write_file("notes", BOUND) and seen == []


def test_a_runner_crash_never_raises(on, monkeypatch):
    def crash(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(file_access.requests, "post", crash)
    assert "nothing was written" in file_access.write_file("notes", BOUND)
