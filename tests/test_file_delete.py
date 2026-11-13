"""Runner side of deleting a file: operation stat (read-only) and operation delete (moves one file into the trash),
the fixed scripts run for real on a temporary folder, the spec, the endpoint switches, and the host-only trash commands
(`ragleap files trash list|restore|empty`). No Docker, network or database."""
import hashlib
import http.client
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "ragleap_files.py"
_spec = importlib.util.spec_from_file_location("sandbox_runner_delete", ROOT / "sandbox" / "runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

WRITE = {"name": "notes", "path": "/data/notes", "mode": "write", "purpose": "use", "expires": None}
READ = dict(WRITE, name="ro", mode="read")


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


# ---------------- the specs ----------------

def test_the_delete_spec_has_exactly_one_writable_mount_and_the_same_hardening():
    spec = runner.build_delete_spec(WRITE, "a.txt", "a" * 64)
    assert spec["HostConfig"]["Mounts"] == [{"Type": "bind", "Source": "/data/notes", "Target": "/data", "ReadOnly": False}]
    hc = spec["HostConfig"]
    assert spec["User"] == "65534:65534" and spec["NetworkDisabled"] is True and hc["NetworkMode"] == "none"
    assert hc["ReadonlyRootfs"] is True and hc["CapDrop"] == ["ALL"] and hc["Privileged"] is False
    assert "no-new-privileges" in hc["SecurityOpt"] and "Binds" not in hc
    assert spec["Cmd"] == ["python", "-I", "-B", "-c", runner.FILE_DELETER]
    assert sorted(e.split("=", 1)[0] for e in spec["Env"]) == ["EXPECT", "OLDMAX", "REL", "TRASHMAX"]


def test_a_stat_spec_is_read_only_and_the_other_specs_are_unchanged():
    assert "stat" in runner.FILE_OPS and "delete" in runner.FILE_OPS
    spec = runner.build_file_spec(WRITE, "stat", "a.txt")
    assert [m["ReadOnly"] for m in spec["HostConfig"]["Mounts"]] == [True]
    assert sorted(e.split("=", 1)[0] for e in spec["Env"]) == ["MAXB", "MAXN", "OLDMAX", "OP", "REL"]
    plain = runner.build_file_spec(WRITE, "read", "a.txt")
    assert sorted(e.split("=", 1)[0] for e in plain["Env"]) == ["MAXB", "MAXN", "OP", "REL"]


# ---------------- the scripts, run for real ----------------

@pytest.fixture
def base(tmp_path):
    b = tmp_path / "notes"
    (b / "sub").mkdir(parents=True)
    (b / "a.txt").write_text("one\ntwo\n")
    (b / "sub" / "deep.txt").write_text("deep\n")
    (b / "perm.txt").write_text("keep my mode\n")
    os.chmod(b / "perm.txt", 0o640)
    (b / "bin.dat").write_bytes(b"\x00\x01binary")
    (b / "big.txt").write_text("x" * 5000)
    (b / ".env").write_text("SECRET=1\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    os.symlink(str(outside), str(b / "link.txt"))
    os.symlink(str(tmp_path), str(b / "linkdir"))
    os.symlink(str(b / "a.txt"), str(b / "inlink.txt"))
    return b


def stat(base, rel, **env):
    e = dict(os.environ, RL_DATA=str(base), OP="stat", REL=rel, MAXB="12000", MAXN="100", OLDMAX=str(runner.PREVIEW_OLD_MAX))
    e.update(env)
    p = subprocess.run([sys.executable, "-I", "-B", "-c", runner.FILE_READER], env=e, capture_output=True, text=True, timeout=30)
    return p.returncode, json.loads(p.stdout)


def delete(base, rel, expect, **env):
    e = dict(os.environ, RL_DATA=str(base), REL=rel, EXPECT=expect, OLDMAX=str(runner.PREVIEW_OLD_MAX),
             TRASHMAX=str(runner.WRITE_TRASH_MAX))
    e.update(env)
    p = subprocess.run([sys.executable, "-I", "-B", "-c", runner.FILE_DELETER], env=e, capture_output=True, text=True, timeout=30)
    return p.returncode, json.loads(p.stdout)


def trash_dirs(base):
    t = base / ".ragleap-trash"
    return sorted(os.listdir(t)) if t.exists() else []


def test_stat_shows_size_hash_and_the_start_and_changes_nothing(base):
    rc, d = stat(base, "a.txt")
    assert rc == 0 and d["exists"] is True and d["size"] == 8 and d["sha256"] == sha("one\ntwo\n")
    assert d["binary"] is False and d["head"] == "one\ntwo\n"
    assert (base / "a.txt").read_text() == "one\ntwo\n" and trash_dirs(base) == []


def test_stat_of_binary_missing_and_odd_targets(base):
    rc, d = stat(base, "bin.dat")
    assert rc == 0 and d["binary"] is True and d["head"] == "" and d["sha256"] == sha(b"\x00\x01binary")
    assert stat(base, "nope.txt") == (0, {"op": "stat", "exists": False})
    for rel, want in (("sub", "not a file"), ("", "not a file"), ("link.txt", "outside the granted folder"),
                      ("linkdir/outside.txt", "outside the granted folder"), ("inlink.txt", "symbolic link"),
                      (".env", "not allowed"), ("../x", "not allowed")):
        rc, d = stat(base, rel)
        assert rc != 0 and want in d["error"], (rel, d)


def test_stat_refuses_a_big_file_and_cuts_the_head(base):
    rc, d = stat(base, "big.txt", OLDMAX="1000")
    assert rc != 0 and "too large" in d["error"]
    rc, d = stat(base, "big.txt")
    assert rc == 0 and len(d["head"]) == 300


def test_a_delete_moves_the_file_into_the_trash_with_content_and_mode(base):
    rc, d = delete(base, "perm.txt", sha("keep my mode\n"))
    assert rc == 0 and d["deleted"] is True and d["old_sha256"] == sha("keep my mode\n") and d["size"] == 13
    assert not (base / "perm.txt").exists()
    stamps = trash_dirs(base)
    assert len(stamps) == 1 and d["trash"] == ".ragleap-trash/%s/perm.txt" % stamps[0]
    kept = base / d["trash"]
    assert kept.read_text() == "keep my mode\n" and oct(kept.stat().st_mode & 0o777) == oct(0o640)
    assert (base / "a.txt").exists()


def test_a_nested_file_and_a_binary_file_can_be_deleted(base):
    rc, d = delete(base, "sub/deep.txt", sha("deep\n"))
    assert rc == 0 and d["trash"].endswith("/sub/deep.txt") and (base / d["trash"]).read_text() == "deep\n"
    rc, d = delete(base, "bin.dat", sha(b"\x00\x01binary"))
    assert rc == 0 and (base / d["trash"]).read_bytes() == b"\x00\x01binary"


def test_a_file_that_changed_since_the_preview_is_not_touched(base):
    rc, d = delete(base, "a.txt", sha("something else"))
    assert rc == 4 and "changed since the preview" in d["error"]
    assert (base / "a.txt").read_text() == "one\ntwo\n" and trash_dirs(base) == []


def test_a_missing_file_a_folder_and_bad_hashes_are_refused(base):
    rc, d = delete(base, "nope.txt", "a" * 64)
    assert rc == 4 and "gone" in d["error"]
    rc, d = delete(base, "sub", "a" * 64)
    assert rc != 0 and "not a file" in d["error"] and (base / "sub").is_dir()
    rc, d = delete(base, "", "a" * 64)
    assert rc != 0 and "not a file" in d["error"]


@pytest.mark.parametrize("rel,expect", [
    ("link.txt", "symbolic link"), ("linkdir/outside.txt", "symbolic link"), ("inlink.txt", "symbolic link"),
    (".env", "not allowed"), ("../x", "not allowed"),
    (".ragleap-trash/x/a.txt", "not allowed"), ("sub/.ragleap-tmp-1", "not allowed"),
])
def test_paths_that_could_leave_or_hide_things_are_refused(base, tmp_path, rel, expect):
    rc, d = delete(base, rel, "a" * 64)
    assert rc != 0 and expect in d["error"]
    assert (tmp_path / "outside.txt").read_text() == "outside\n" and (base / ".env").exists()


def test_a_big_file_is_refused_not_cut(base):
    rc, d = delete(base, "big.txt", sha("x" * 5000), OLDMAX="1000")
    assert rc != 0 and "too large" in d["error"] and (base / "big.txt").exists()


def test_a_full_trash_refuses_and_keeps_the_file_and_leaves_no_empty_folder(base):
    rc, d = delete(base, "a.txt", sha("one\ntwo\n"))
    assert rc == 0
    before = trash_dirs(base)
    rc, d = delete(base, "perm.txt", sha("keep my mode\n"), TRASHMAX="1")
    assert rc == 4 and "trash is full" in d["error"]
    assert (base / "perm.txt").exists() and trash_dirs(base) == before


def test_a_failed_delete_leaves_no_empty_stamp_folder(base):
    rc, d = delete(base, "sub/deep.txt", "b" * 64)
    assert rc == 4 and trash_dirs(base) == []


def test_the_trash_is_invisible_to_the_reader_after_a_delete(base):
    delete(base, "a.txt", sha("one\ntwo\n"))
    e = dict(os.environ, RL_DATA=str(base), OP="list", REL="", MAXB="12000", MAXN="100")
    p = subprocess.run([sys.executable, "-I", "-B", "-c", runner.FILE_READER], env=e, capture_output=True, text=True, timeout=30)
    names = [x["name"] for x in json.loads(p.stdout)["entries"]]
    assert ".ragleap-trash" not in names and "a.txt" not in names and "perm.txt" in names


# ---------------- the endpoint ----------------

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
        if spec["Cmd"][-1] == runner.FILE_DELETER:
            out = {"op": "delete", "deleted": True, "old_sha256": "a" * 64, "size": 5, "trash": ".ragleap-trash/x/a.txt"}
        else:
            out = {"op": "stat", "exists": True, "size": 5, "sha256": "a" * 64, "binary": False, "head": "hello"}
        return {"exit_code": 0, "stdout": json.dumps(out), "stderr": "", "timed_out": False, "oom": False}
    monkeypatch.setattr(runner, "_run_spec", fake_run)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), runner.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def call(body):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/files", body=json.dumps(body), headers={"Content-Type": "application/json", "Authorization": "Bearer tok"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data or b"{}")
    yield call, calls, tmp_path
    httpd.shutdown()
    httpd.server_close()


GOOD = {"op": "delete", "grant": "notes", "path": "a.txt", "expect_sha256": "a" * 64}


def test_delete_is_off_until_writing_is_switched_on_at_the_host(srv):
    call, calls, tmp = srv
    status, body = call(GOOD)
    assert status == 403 and body["error"] == "writing is off" and calls == []
    (tmp / "WRITE_ENABLED").write_text("on\n")
    status, body = call(GOOD)
    assert status == 200 and body["ok"] is True and body["result"]["deleted"] is True
    assert len(calls) == 1 and calls[0]["HostConfig"]["Mounts"][0]["ReadOnly"] is False and len(calls[0]["HostConfig"]["Mounts"]) == 1


def test_the_pause_flag_and_computer_control_still_win(srv, monkeypatch):
    call, calls, tmp = srv
    (tmp / "WRITE_ENABLED").write_text("on\n")
    (tmp / "DISABLED").write_text("paused\n")
    assert call(GOOD)[0] == 403
    (tmp / "DISABLED").unlink()
    monkeypatch.setenv("COMPUTER_CONTROL", "")
    assert call(GOOD)[0] == 403 and calls == []


def test_a_read_grant_and_bad_requests_are_refused_before_any_container(srv):
    call, calls, tmp = srv
    (tmp / "WRITE_ENABLED").write_text("on\n")
    assert call(dict(GOOD, grant="ro"))[0] == 403
    for bad in (dict(GOOD, expect_sha256="nope"), dict(GOOD, expect_sha256="-"), dict(GOOD, expect_sha256=None),
                {k: v for k, v in GOOD.items() if k != "expect_sha256"}, dict(GOOD, path=""), dict(GOOD, path="../x")):
        assert call(bad)[0] == 400, bad
    assert calls == []


def test_stat_needs_a_write_grant_but_not_the_writing_switch(srv):
    call, calls, tmp = srv
    status, body = call({"op": "stat", "grant": "notes", "path": "a.txt"})
    assert status == 200 and body["result"]["exists"] is True and calls[0]["HostConfig"]["Mounts"][0]["ReadOnly"] is True
    assert call({"op": "stat", "grant": "ro", "path": "a.txt"})[0] == 403 and len(calls) == 1


def test_the_delete_audit_line_has_hashes_and_the_trash_name(srv, capsys):
    call, calls, tmp = srv
    (tmp / "WRITE_ENABLED").write_text("on\n")
    call(GOOD)
    out = capsys.readouterr().out
    line = [json.loads(x) for x in out.splitlines() if x.startswith("{") and '"file_delete"' in x][0]
    assert line["grant"] == "notes" and line["ok"] is True and line["old_sha256"] == "a" * 64 and line["trash"]


# ---------------- the host commands ----------------

def cli(home, *args):
    p = subprocess.run([sys.executable, str(HELPER), *args],
                       env=dict(os.environ, RAGLEAP_HOME=str(home)), capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture
def host(tmp_path, base):
    home = tmp_path / "home"
    (home / "grants").mkdir(parents=True)
    (home / "grants" / "grants.json").write_text(json.dumps({"version": 1, "grants": [
        {"name": "notes", "path": str(base), "mode": "write", "purpose": "use", "expires": None}]}))
    return home


def test_trash_list_restore_after_a_real_delete(host, base):
    assert cli(host, "trash", "list", "notes")[1].startswith("The trash of 'notes' is empty")
    rc, d = delete(base, "sub/deep.txt", sha("deep\n"))
    stamp = d["trash"].split("/")[1]
    rc, out, err = cli(host, "trash", "list", "notes")
    assert rc == 0 and stamp in out and "sub/deep.txt" in out and "5 bytes" in out
    rc, out, err = cli(host, "trash", "restore", "notes", stamp)
    assert rc == 0 and "Restored sub/deep.txt" in out
    assert (base / "sub" / "deep.txt").read_text() == "deep\n" and not (base / ".ragleap-trash" / stamp).exists()


def test_restore_refuses_when_a_file_is_there_and_replace_keeps_the_current_one(host, base):
    rc, d = delete(base, "a.txt", sha("one\ntwo\n"))
    stamp = d["trash"].split("/")[1]
    (base / "a.txt").write_text("new content\n")
    rc, out, err = cli(host, "trash", "restore", "notes", stamp)
    assert rc == 1 and "--replace" in err and (base / "a.txt").read_text() == "new content\n"
    rc, out, err = cli(host, "trash", "restore", "notes", stamp, "--replace")
    assert rc == 0 and (base / "a.txt").read_text() == "one\ntwo\n" and "now in the trash" in out
    kept = [e for e in cli(host, "trash", "list", "notes")[1].splitlines() if "a.txt" in e]
    assert len(kept) == 1
    kid = kept[0].split()[0]
    assert (base / ".ragleap-trash" / kid / "a.txt").read_text() == "new content\n"


def test_restore_refuses_unknown_ids_links_and_protected_names(host, base, tmp_path):
    assert cli(host, "trash", "restore", "notes", "20260101T000000Z-aaaaaa")[0] == 1
    assert cli(host, "trash", "restore", "nope", "x")[0] == 1
    rc, d = delete(base, "sub/deep.txt", sha("deep\n"))
    stamp = d["trash"].split("/")[1]
    (base / "sub" / "deep.txt").unlink(missing_ok=True)
    (base / "sub").rmdir() if not os.listdir(base / "sub") else None
    os.symlink(str(tmp_path), str(base / "sub"))
    rc, out, err = cli(host, "trash", "restore", "notes", stamp)
    assert rc == 1 and "link" in err and not (tmp_path / "deep.txt").exists()
    # a stamp holding a protected name is not restored by the command
    fake = base / ".ragleap-trash" / "20260101T000000Z-bbbbbb"
    fake.mkdir(parents=True)
    (fake / ".env").write_text("SECRET=1\n")
    rc, out, err = cli(host, "trash", "restore", "notes", "20260101T000000Z-bbbbbb")
    assert rc == 1 and "protected" in err and (base / ".env").read_text() == "SECRET=1\n"


def test_trash_ignores_links_and_odd_things(host, base, tmp_path):
    t = base / ".ragleap-trash"
    t.mkdir()
    os.symlink(str(tmp_path), str(t / "20260101T000000Z-cccccc"))
    (t / "not-a-stamp").mkdir()
    (t / "not-a-stamp" / "x.txt").write_text("x")
    assert cli(host, "trash", "list", "notes")[1].startswith("The trash of 'notes' is empty")


def test_empty_asks_first_then_removes_and_older_than_keeps_new_ones(host, base, tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y%m%dT%H%M%SZ") + "-aaaaaa"
    (base / ".ragleap-trash" / old).mkdir(parents=True)
    (base / ".ragleap-trash" / old / "old.txt").write_text("old")
    rc, d = delete(base, "a.txt", sha("one\ntwo\n"))
    new = d["trash"].split("/")[1]
    rc, out, err = cli(host, "trash", "empty", "notes", "--older-than", "7d")
    assert rc == 0 and "would remove 1" in out and "Nothing was removed" in out and (base / ".ragleap-trash" / old).exists()
    rc, out, err = cli(host, "trash", "empty", "notes", "--older-than", "7d", "--yes")
    assert rc == 0 and "Removed 1" in out and not (base / ".ragleap-trash" / old).exists() and (base / ".ragleap-trash" / new).exists()
    assert cli(host, "trash", "empty", "notes", "--older-than", "soon")[0] == 1
    rc, out, err = cli(host, "trash", "empty", "notes", "--yes")
    assert rc == 0 and "Removed 1" in out and trash_dirs(base) == [] and (tmp_path / "outside.txt").exists()
    assert "Nothing to remove" in cli(host, "trash", "empty", "notes")[1]


def test_a_write_leaves_a_version_that_trash_restore_brings_back(host, base):
    new = "uno\ndos\n"
    e = dict(os.environ, RL_DATA=str(base), REL="a.txt", NEW=new, EXPECT=sha("one\ntwo\n"), NEWSHA=sha(new),
             OLDMAX=str(runner.PREVIEW_OLD_MAX), TRASHMAX=str(runner.WRITE_TRASH_MAX))
    p = subprocess.run([sys.executable, "-I", "-B", "-c", runner.FILE_WRITER], env=e, capture_output=True, text=True, timeout=30)
    stamp = json.loads(p.stdout)["trash"].split("/")[1]
    assert (base / "a.txt").read_text() == new
    assert cli(host, "trash", "restore", "notes", stamp, "--replace")[0] == 0
    assert (base / "a.txt").read_text() == "one\ntwo\n"


def test_the_docs_page_no_longer_promises_trash_commands_as_future_work():
    text = (ROOT / "docs" / "file-access.md").read_text()
    assert "ragleap files trash restore" in text and "{{" not in text and "{%" not in text
