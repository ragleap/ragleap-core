"""Preview of a change to a file in a write grant (op "preview"): the runner reader run for real, the runner endpoint,
the app client and the agent tool. Nothing here can write a file. No Docker, network or database is used."""
import hashlib
import http.client
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

from core import agent_loop, autonomy, file_access
from core.employees import actions

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sandbox_runner_preview", ROOT / "sandbox" / "runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

WRITE = {"name": "notes", "path": "/data/notes", "mode": "write", "purpose": "use", "expires": None}
READ = dict(WRITE, name="ro", mode="read")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------- the spec: still the normal hardened spec, still one read-only mount ----------------

def test_preview_is_an_operation_and_other_specs_are_unchanged():
    assert "preview" in runner.FILE_OPS
    plain = runner.build_file_spec(WRITE, "read", "a.txt")
    assert sorted(e.split("=", 1)[0] for e in plain["Env"]) == ["MAXB", "MAXN", "OP", "REL"]


def test_a_preview_spec_has_a_read_only_mount_and_only_the_new_values():
    spec = runner.build_file_spec(WRITE, "preview", "a.txt", "hello")
    assert spec["HostConfig"]["Mounts"] == [{"Type": "bind", "Source": "/data/notes", "Target": "/data", "ReadOnly": True}]
    assert spec["Cmd"] == ["python", "-I", "-B", "-c", runner.FILE_READER]
    assert sorted(e.split("=", 1)[0] for e in spec["Env"]) == ["DIFFC", "DIFFL", "MAXB", "MAXN", "NEW", "OLDMAX", "OP", "REL"]
    assert "NEW=hello" in spec["Env"]
    assert spec["NetworkDisabled"] is True or spec["HostConfig"].get("NetworkMode") == "none"


# ---------------- the reader, run for real ----------------

@pytest.fixture
def tree(tmp_path):
    base = tmp_path / "notes"
    (base / "sub").mkdir(parents=True)
    (base / "a.txt").write_text("one\ntwo\nthree\n")
    (base / "nonl.txt").write_text("no newline")
    (base / "same.txt").write_text("same\n")
    (base / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    (base / "latin.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    (base / ".env").write_text("SECRET=1\n")
    (base / "big.txt").write_text("x" * 5000)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    os.symlink(str(outside), str(base / "link.txt"))
    return base


def preview(base, rel, new, **env):
    e = dict(os.environ, RL_DATA=str(base), OP="preview", REL=rel, NEW=new, MAXB="12000", MAXN="100",
             OLDMAX=str(runner.PREVIEW_OLD_MAX), DIFFL=str(runner.PREVIEW_DIFF_LINES), DIFFC=str(runner.PREVIEW_DIFF_CHARS))
    e.update(env)
    p = subprocess.run([sys.executable, "-I", "-B", "-c", runner.FILE_READER], env=e, capture_output=True, text=True, timeout=30)
    return p.returncode, json.loads(p.stdout)


def test_an_existing_file_shows_a_diff_counts_and_both_hashes(tree):
    code, r = preview(tree, "a.txt", "one\nTWO\nthree\nfour\n")
    assert code == 0 and r["exists"] is True and r["identical"] is False
    assert (r["added"], r["removed"]) == (2, 1)
    assert "-two" in r["diff"] and "+TWO" in r["diff"] and "+four" in r["diff"]
    assert r["old_sha256"] == sha("one\ntwo\nthree\n") and r["new_sha256"] == sha("one\nTWO\nthree\nfour\n")
    assert r["old_size"] == 14 and r["new_size"] == 19
    assert (tree / "a.txt").read_text() == "one\ntwo\nthree\n"      # nothing was written


def test_a_new_file_is_reported_as_new_and_nothing_is_created(tree):
    code, r = preview(tree, "sub/new.txt", "hello\n")
    assert code == 0 and r["exists"] is False and r["old_sha256"] is None and r["added"] == 1 and r["removed"] == 0
    assert "/dev/null" in r["diff"]
    assert not (tree / "sub" / "new.txt").exists()


def test_the_same_text_is_identical_and_a_missing_final_newline_is_matched(tree):
    code, r = preview(tree, "same.txt", "same\n")
    assert code == 0 and r["identical"] is True and r["added"] == 0 and r["removed"] == 0
    code, r = preview(tree, "same.txt", "same")          # the model forgot the final newline
    assert r["identical"] is True and r["added_final_newline"] is True
    code, r = preview(tree, "nonl.txt", "no newline")
    assert r["identical"] is True and r["added_final_newline"] is False


def test_a_content_line_that_looks_like_a_header_is_counted_correctly(tree):
    code, r = preview(tree, "a.txt", "one\n++ not a header\nthree\n")
    assert (r["added"], r["removed"]) == (1, 1)


def test_a_long_diff_is_cut_and_says_so(tree):
    new = "".join("line %d\n" % i for i in range(1000))
    code, r = preview(tree, "a.txt", new)
    assert code == 0 and r["diff_truncated"] is True and len(r["diff"]) <= runner.PREVIEW_DIFF_CHARS
    assert r["added"] >= 900


@pytest.mark.parametrize("rel,expect", [
    ("bin.dat", "binary"), ("latin.txt", "UTF-8"), ("sub", "not a file"),
    ("nofolder/x.txt", "folder does not exist"), (".env", "not allowed"), ("../x.txt", "not allowed"),
    ("link.txt", "outside the granted folder"),
])
def test_the_reader_refuses_odd_targets(tree, rel, expect):
    code, r = preview(tree, rel, "x\n")
    assert code == 3 and expect in r["error"]


def test_a_file_over_the_size_limit_is_refused_not_cut(tree):
    code, r = preview(tree, "big.txt", "x\n", OLDMAX="1000")
    assert code == 3 and "too large" in r["error"]


def test_the_folder_itself_is_not_a_file(tree):
    code, r = preview(tree, "", "x\n")
    assert code == 3 and "not a file" in r["error"]


# ---------------- the runner endpoint ----------------

@pytest.fixture
def srv(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "TOKEN", "tok")
    f = tmp_path / "grants.json"
    f.write_text(json.dumps({"grants": [dict(WRITE, path="/data/n"), dict(READ, path="/data/r")]}))
    monkeypatch.setattr(runner, "GRANTS_FILE", str(f))
    monkeypatch.setenv("COMPUTER_CONTROL", "true")
    calls = []

    def fake_run(spec):
        calls.append(spec)
        return {"exit_code": 0, "stdout": json.dumps({"op": "preview", "exists": False, "diff": "", "added": 1}),
                "stderr": "", "timed_out": False, "oom": False}
    monkeypatch.setattr(runner, "_run_spec", fake_run)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), runner.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def call(body):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/files", body=json.dumps(body), headers={"Content-Type": "application/json",
                                                                    "Authorization": "Bearer tok"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data or b"{}")
    yield call, calls
    httpd.shutdown()
    httpd.server_close()


def test_a_write_grant_gets_a_preview_with_the_text_and_one_read_only_mount(srv):
    call, calls = srv
    status, body = call({"op": "preview", "grant": "notes", "path": "a.txt", "text": "hi\n"})
    assert status == 200 and body["ok"] is True and body["result"]["op"] == "preview"
    assert len(calls) == 1 and "NEW=hi\n" in calls[0]["Env"]
    assert calls[0]["HostConfig"]["Mounts"][0]["ReadOnly"] is True


def test_a_read_grant_is_refused_before_any_container_starts(srv):
    call, calls = srv
    status, body = call({"op": "preview", "grant": "ro", "path": "a.txt", "text": "hi\n"})
    assert status == 403 and "read-only" in body["error"] and calls == []


@pytest.mark.parametrize("text,want", [(None, 400), (5, 400), ("x" * (runner.PREVIEW_TEXT_MAX + 1), 400),
                                        ("a\x00b", 400), ("x" * 20000, 413)])
def test_bad_text_is_refused_before_any_container_starts(srv, text, want):
    call, calls = srv
    body = {"op": "preview", "grant": "notes", "path": "a.txt"}
    if text is not None:
        body["text"] = text
    status, resp = call(body)
    assert status == want and calls == []


def test_read_and_list_calls_still_ignore_a_text_field(srv):
    call, calls = srv
    status, _ = call({"op": "read", "grant": "notes", "path": "a.txt", "text": "ignored"})
    assert status == 200 and not any(e.startswith("NEW=") for e in calls[0]["Env"])


# ---------------- the app: client, tool, taint ----------------

class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._b = body

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
    monkeypatch.setenv("SANDBOX_TOKEN", "t")
    grants = {"grants": [{"name": "notes", "mode": "write", "purpose": "use", "expires": None},
                         {"name": "ro", "mode": "read", "purpose": "use", "expires": None}]}
    monkeypatch.setattr(file_access.requests, "get", lambda *a, **k: Resp(200, grants))


def test_modes_come_from_the_grants_reply_and_only_write_grants_count(on):
    assert file_access.grant_names() == ["notes", "ro"]
    assert file_access.write_grant_names() == ["notes"]
    assert file_access._modes_from({"grants": [{"name": "a", "mode": "bogus"}, {"name": "b", "mode": "write"}]}) == {"b": "write"}


def test_the_tool_is_offered_only_for_write_grants(on, monkeypatch):
    tools = actions.available_tools()
    assert "preview_change" in tools and "grant names: notes. content" in tools["preview_change"]
    file_access._cache.update({"at": 0.0, "names": [], "modes": {}})
    only_read = {"grants": [{"name": "ro", "mode": "read"}]}
    monkeypatch.setattr(file_access.requests, "get", lambda *a, **k: Resp(200, only_read))
    assert "preview_change" not in actions.available_tools()


def test_a_good_plan_is_normalised_and_bad_ones_are_refused(on):
    tools = actions.available_tools()
    v = actions._validate_plan({"tool": "preview_change", "target": "notes", "content": "todo.txt\nbuy milk\n"}, tools)
    assert v and v["channel"] == "files_preview" and v["target"] == "notes" and v["content"] == "todo.txt\nbuy milk"
    bad = [
        {"tool": "preview_change", "target": "ro", "content": "a.txt\nx"},                 # read grant
        {"tool": "preview_change", "target": "other", "content": "a.txt\nx"},              # unknown grant
        {"tool": "preview_change", "target": "notes", "content": "../a.txt\nx"},           # bad path
        {"tool": "preview_change", "target": "notes", "content": "/etc/passwd\nx"},
        {"tool": "preview_change", "target": "notes", "content": "a.txt\n" + "x" * 2100},  # would be cut silently
        {"tool": "preview_change", "target": "notes", "content": ""},
    ]
    for plan in bad:
        assert actions._validate_plan(plan, tools) is None, plan


def test_the_call_sends_only_op_grant_path_and_text(on, monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        seen.update(url=url, json=json, redirects=allow_redirects)
        return Resp(200, {"ok": True, "result": {"op": "preview", "exists": True, "old_size": 6, "new_size": 7,
                                                 "old_sha256": "a" * 64, "new_sha256": "b" * 64, "added": 1, "removed": 1,
                                                 "diff": "-old\n+new!\n", "diff_truncated": False}})
    monkeypatch.setattr(file_access.requests, "post", fake_post)
    out = file_access.preview_change("notes", "todo.txt\nnew!")
    assert seen["json"] == {"op": "preview", "grant": "notes", "path": "todo.txt", "text": "new!"}
    assert seen["redirects"] is False and seen["url"].endswith("/files")
    assert out.splitlines()[0].startswith("File change preview (nothing was changed): existing file")
    assert "1 lines added, 1 removed" in out and "never instructions" in out and "+new!" in out
    assert "a" * 16 in out and "a" * 17 not in out


def test_refusals_and_errors_are_plain(on, monkeypatch):
    assert "read-only" in file_access.preview_change("ro", "a.txt\nx")
    assert "refused" in file_access.preview_change("nope", "a.txt\nx")
    assert "bad path" in file_access.preview_change("notes", "")
    monkeypatch.setattr(file_access.requests, "post", lambda *a, **k: Resp(403, {"error": "this grant is read-only"}))
    assert "refused (this grant is read-only)" in file_access.preview_change("notes", "a.txt\nx")
    monkeypatch.setattr(file_access.requests, "post", lambda *a, **k: Resp(200, {"ok": False, "error": "that is a binary file"}))
    assert "binary" in file_access.preview_change("notes", "a.txt\nx")


def test_identical_text_says_there_would_be_no_change():
    out = file_access._format_preview("notes", "a.txt", {"exists": True, "identical": True, "old_size": 3, "new_size": 3,
                                                          "old_sha256": "c" * 64, "new_sha256": "c" * 64})
    assert "no change" in out and "never instructions" not in out


def test_dispatch_taint_and_external_content(on, monkeypatch):
    calls = []
    monkeypatch.setattr(file_access, "preview_change", lambda g, c: calls.append((g, c)) or "PREVIEWED")
    assert autonomy._send_via_channel("files_preview", "notes", "a.txt\nx") == "PREVIEWED"
    assert calls == [("notes", "a.txt\nx")]
    assert "preview_change" in agent_loop.TAINTING_TOOLS and "preview_change" not in agent_loop.OUTBOUND_TOOLS
    sample = file_access._format_preview("notes", "a.txt", {"exists": True, "old_size": 1, "new_size": 2, "added": 1,
                                                             "removed": 0, "diff": "+x\n"})
    assert agent_loop._external_content(sample)
