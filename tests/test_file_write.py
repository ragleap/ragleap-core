"""Writing to a file in a write grant (op "write"): the fixed writer run for real on a temporary folder, the spec, the
runner endpoint, the trash, and the host-only switch (`ragleap files writing`). No Docker, network or database."""
import hashlib
import http.client
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "ragleap_files.py"
_spec = importlib.util.spec_from_file_location("sandbox_runner_write", ROOT / "sandbox" / "runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

WRITE = {"name": "notes", "path": "/data/notes", "mode": "write", "purpose": "use", "expires": None}
READ = dict(WRITE, name="ro", mode="read")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------- the spec ----------------

def test_the_write_spec_has_exactly_one_writable_mount_and_the_same_hardening():
    spec = runner.build_write_spec(WRITE, "a.txt", "hi\n", "-", sha("hi\n"))
    assert spec["HostConfig"]["Mounts"] == [{"Type": "bind", "Source": "/data/notes", "Target": "/data", "ReadOnly": False}]
    hc = spec["HostConfig"]
    assert spec["User"] == "65534:65534" and spec["NetworkDisabled"] is True and hc["NetworkMode"] == "none"
    assert hc["ReadonlyRootfs"] is True and hc["CapDrop"] == ["ALL"] and hc["Privileged"] is False
    assert "no-new-privileges" in hc["SecurityOpt"] and "Binds" not in hc
    assert spec["Cmd"] == ["python", "-I", "-B", "-c", runner.FILE_WRITER]
    assert sorted(e.split("=", 1)[0] for e in spec["Env"]) == ["EXPECT", "NEW", "NEWSHA", "OLDMAX", "REL", "TRASHMAX"]


@pytest.mark.parametrize("op", ["list", "read", "preview"])
def test_every_other_file_spec_is_still_read_only(op):
    spec = runner.build_file_spec(WRITE, op, "a.txt", "x" if op == "preview" else None)
    assert [m["ReadOnly"] for m in spec["HostConfig"]["Mounts"]] == [True]


def test_the_trash_and_temp_names_are_protected_everywhere():
    assert runner.name_denied(".ragleap-trash") and runner.name_denied(".ragleap-tmp-abc")
    assert ".ragleap-" in runner.DENY_PREFIXES


# ---------------- the writer, run for real ----------------

@pytest.fixture
def base(tmp_path):
    b = tmp_path / "notes"
    (b / "sub").mkdir(parents=True)
    (b / "a.txt").write_text("one\ntwo\n")
    (b / "perm.txt").write_text("keep my mode\n")
    os.chmod(b / "perm.txt", 0o640)
    (b / "bin.dat").write_bytes(b"\x00\x01binary")
    (b / "latin.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    (b / "big.txt").write_text("x" * 5000)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    os.symlink(str(outside), str(b / "link.txt"))
    os.symlink(str(tmp_path), str(b / "linkdir"))
    return b


def write(base, rel, new, expect, newsha=None, **env):
    e = dict(os.environ, RL_DATA=str(base), REL=rel, NEW=new, EXPECT=expect, NEWSHA=newsha if newsha is not None else sha(new),
             OLDMAX=str(runner.PREVIEW_OLD_MAX), TRASHMAX=str(runner.WRITE_TRASH_MAX))
    e.update(env)
    p = subprocess.run([sys.executable, "-I", "-B", "-c", runner.FILE_WRITER], env=e, capture_output=True, text=True, timeout=30)
    return p.returncode, json.loads(p.stdout)


def leftovers(base):
    return [p.name for p in base.rglob(".ragleap-tmp-*")]


def test_a_new_file_is_created_with_no_trash(base):
    code, r = write(base, "sub/new.txt", "hello\n", "-")
    assert code == 0 and r["changed"] is True and r["created"] is True and r["old_sha256"] is None
    assert r["new_sha256"] == sha("hello\n") and r["trash"] is None and r["bytes"] == 6
    assert (base / "sub" / "new.txt").read_text() == "hello\n"
    assert stat.S_IMODE((base / "sub" / "new.txt").stat().st_mode) == 0o644
    assert not (base / ".ragleap-trash").exists() and leftovers(base) == []


def test_an_overwrite_keeps_the_old_version_in_the_trash_and_the_mode(base):
    old = (base / "perm.txt").read_text()
    code, r = write(base, "perm.txt", "new text\n", sha(old))
    assert code == 0 and r["changed"] is True and r["created"] is False and r["old_sha256"] == sha(old)
    assert (base / "perm.txt").read_text() == "new text\n"
    assert stat.S_IMODE((base / "perm.txt").stat().st_mode) == 0o640
    kept = base / r["trash"]
    assert r["trash"].startswith(".ragleap-trash/") and r["trash"].endswith("/perm.txt")
    assert kept.read_text() == old and leftovers(base) == []


def test_a_file_that_changed_since_the_preview_is_not_touched(base):
    code, r = write(base, "a.txt", "mine\n", sha("something else\n"))
    assert code == 4 and "changed since the preview" in r["error"]
    assert (base / "a.txt").read_text() == "one\ntwo\n" and not (base / ".ragleap-trash").exists()


def test_new_versus_existing_mismatches_are_refused(base):
    code, r = write(base, "a.txt", "mine\n", "-")                 # the preview said "new", but it exists now
    assert code == 4 and "exists now" in r["error"] and (base / "a.txt").read_text() == "one\ntwo\n"
    code, r = write(base, "gone.txt", "mine\n", sha("old\n"))      # the preview said "exists", but it is gone
    assert code == 4 and "gone" in r["error"] and not (base / "gone.txt").exists()


def test_text_that_is_not_the_previewed_text_is_refused_before_the_trash(base):
    old = (base / "a.txt").read_text()
    code, r = write(base, "a.txt", "mine\n", sha(old), newsha=sha("other\n"))
    assert code == 4 and "not the text that was previewed" in r["error"]
    assert (base / "a.txt").read_text() == old and not (base / ".ragleap-trash").exists()


def test_a_missing_final_newline_is_added_like_the_preview_does(base):
    old = (base / "a.txt").read_text()                             # ends with a newline
    code, r = write(base, "a.txt", "changed", sha(old), newsha=sha("changed\n"))
    assert code == 0 and (base / "a.txt").read_text() == "changed\n"
    code, r = write(base, "sub/x.txt", "nonl", "-", newsha=sha("nonl"))      # a new file: nothing to match
    assert code == 0 and (base / "sub" / "x.txt").read_text() == "nonl"


def test_identical_text_changes_nothing_and_leaves_no_trash(base):
    old = (base / "a.txt").read_text()
    code, r = write(base, "a.txt", old, sha(old))
    assert code == 0 and r["changed"] is False and r["trash"] is None
    assert not (base / ".ragleap-trash").exists()


@pytest.mark.parametrize("rel,expect", [("bin.dat", "binary"), ("latin.txt", "UTF-8"), ("sub", "not a file"), ("", "not a file")])
def test_odd_existing_targets_are_refused_unchanged(base, rel, expect):
    code, r = write(base, rel, "x\n", sha("whatever"))
    assert code == 3 and expect in r["error"]
    assert (base / "bin.dat").read_bytes() == b"\x00\x01binary"


def test_a_big_old_file_is_refused_not_cut(base):
    code, r = write(base, "big.txt", "x\n", sha("x" * 5000), OLDMAX="1000")
    assert code == 3 and "too large" in r["error"] and (base / "big.txt").read_text() == "x" * 5000


@pytest.mark.parametrize("rel,expect", [
    ("../evil.txt", "not allowed"), (".env", "not allowed"), ("sub/.env", "not allowed"), ("key.pem", "not allowed"),
    (".ragleap-trash/x.txt", "not allowed"), (".ragleap-tmp-1", "not allowed"),
    ("link.txt", "symbolic link"), ("linkdir/evil.txt", "symbolic link"), ("nofolder/x.txt", "folder does not exist"),
])
def test_paths_that_could_leave_or_hide_things_are_refused(base, tmp_path, rel, expect):
    code, r = write(base, rel, "x\n", "-")
    assert code == 3 and expect in r["error"]
    assert (tmp_path / "outside.txt").read_text() == "outside\n" and not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "x.txt").exists()


def test_a_full_trash_refuses_the_write_and_keeps_the_file(base):
    first = (base / "a.txt").read_text()
    code, r = write(base, "a.txt", "v2\n", sha(first))
    assert code == 0
    code, r = write(base, "a.txt", "v3\n", sha("v2\n"), TRASHMAX="1")
    assert code == 4 and "trash is full" in r["error"] and (base / "a.txt").read_text() == "v2\n"


def test_the_trash_is_invisible_to_the_reader(base):
    code, r = write(base, "a.txt", "v2\n", sha("one\ntwo\n"))
    e = dict(os.environ, RL_DATA=str(base), OP="list", REL="", MAXB="12000", MAXN="100")
    p = subprocess.run([sys.executable, "-I", "-B", "-c", runner.FILE_READER], env=e, capture_output=True, text=True, timeout=30)
    names = [x["name"] for x in json.loads(p.stdout)["entries"]]
    assert "a.txt" in names and ".ragleap-trash" not in names
    e.update(OP="read", REL=r["trash"])
    p = subprocess.run([sys.executable, "-I", "-B", "-c", runner.FILE_READER], env=e, capture_output=True, text=True, timeout=30)
    assert p.returncode == 3 and "not allowed" in json.loads(p.stdout)["error"]


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
        return {"exit_code": 0, "stdout": json.dumps({"op": "write", "changed": True, "created": False, "old_sha256": "a" * 64,
                                                      "new_sha256": "b" * 64, "bytes": 5, "trash": ".ragleap-trash/x/a.txt"}),
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
    yield call, calls, tmp_path
    httpd.shutdown()
    httpd.server_close()


GOOD = {"op": "write", "grant": "notes", "path": "a.txt", "text": "hi\n", "expect_sha256": "-", "new_sha256": "c" * 64}


def test_writing_is_off_until_the_owner_switches_it_on_at_the_host(srv):
    call, calls, tmp = srv
    assert not runner.file_write_on()
    status, body = call(GOOD)
    assert status == 403 and body["error"] == "writing is off" and calls == []
    (tmp / "WRITE_ENABLED").write_text("on\n")
    assert runner.file_write_on()
    status, body = call(GOOD)
    assert status == 200 and body["ok"] is True and body["result"]["trash"].startswith(".ragleap-trash/")
    assert len(calls) == 1 and calls[0]["HostConfig"]["Mounts"][0]["ReadOnly"] is False
    assert len(calls[0]["HostConfig"]["Mounts"]) == 1


def test_the_switch_needs_computer_control_and_the_pause_flag_wins(srv, monkeypatch):
    call, calls, tmp = srv
    (tmp / "WRITE_ENABLED").write_text("on\n")
    monkeypatch.setenv("COMPUTER_CONTROL", "")
    assert not runner.file_write_on() and call(GOOD)[0] == 403
    monkeypatch.setenv("COMPUTER_CONTROL", "true")
    (tmp / "DISABLED").write_text("paused\n")
    assert not runner.file_write_on() and call(GOOD)[0] == 403
    assert calls == []


def test_a_read_grant_and_bad_requests_are_refused_before_any_container(srv):
    call, calls, tmp = srv
    (tmp / "WRITE_ENABLED").write_text("on\n")
    assert call(dict(GOOD, grant="ro"))[0] == 403
    for bad in (dict(GOOD, expect_sha256="nope"), dict(GOOD, new_sha256="nope"), dict(GOOD, new_sha256=None),
                {k: v for k, v in GOOD.items() if k != "text"}, dict(GOOD, text="a\x00b"), dict(GOOD, text="x" * 8001),
                dict(GOOD, path="")):
        assert call(bad)[0] == 400, bad
    assert calls == []


def test_the_write_audit_line_has_hashes_and_no_text(srv, capsys):
    call, calls, tmp = srv
    (tmp / "WRITE_ENABLED").write_text("on\n")
    call(dict(GOOD, text="very private text\n"))
    out = capsys.readouterr().out
    line = [json.loads(x) for x in out.splitlines() if x.startswith("{") and '"file_write"' in x][0]
    assert line["grant"] == "notes" and line["ok"] is True and line["new_sha256"] == "b" * 64 and line["trash"]
    assert "very private text" not in out


def test_file_calls_do_not_refuse_the_next_call_as_busy(srv):
    call, calls, tmp = srv
    assert [call({"op": "read", "grant": "notes", "path": "a.txt"})[0] for _ in range(40)] == [200] * 40


# ---------------- the host command ----------------

HELPER_ENV = "RAGLEAP_HOME"


def run(home, *args):
    p = subprocess.run([sys.executable, str(HELPER), *args], env=dict(os.environ, **{HELPER_ENV: str(home)}),
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def test_writing_on_and_off_create_and_remove_the_flag(tmp_path):
    home = tmp_path / "install"
    home.mkdir()
    (home / "docker-compose.yml").write_text("services: {}\n")
    assert "no" in run(home, "status")[1].split("writing on")[1].splitlines()[0]
    rc, out, err = run(home, "writing", "on")
    assert rc == 0 and (home / "grants" / "WRITE_ENABLED").exists() and "approval" in out
    assert "yes" in run(home, "status")[1].split("writing on")[1].splitlines()[0]
    rc, out, err = run(home, "writing", "off")
    assert rc == 0 and not (home / "grants" / "WRITE_ENABLED").exists()
    assert run(home, "writing", "maybe")[0] != 0
