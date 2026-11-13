"""Broad folders (a home folder, /opt, /mnt, /srv, /media): granted only read-only, for use, for at most 24 hours, with
stricter name rules inside the container. The grant rules, the spec, the reader run for real, and the host command
(`ragleap files add --broad --for`). No Docker, network or database."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "ragleap_files.py"
_spec = importlib.util.spec_from_file_location("sandbox_runner_broad", ROOT / "sandbox" / "runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def when(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()


def grant(path="/home/ann", **kw):
    g = {"name": "tmp-home", "path": path, "mode": "read", "purpose": "use", "expires": when(2), "broad": True}
    g.update(kw)
    return g


# ---------------- the grant rules ----------------

@pytest.mark.parametrize("path", ["/home/ann", "/Users/ann", "/opt", "/mnt", "/srv", "/media"])
def test_a_broad_folder_is_accepted_only_with_the_flag_and_a_short_expiry(path):
    g, why = runner.parse_grant(grant(path))
    assert why is None and g["broad"] is True and g["mode"] == "read"
    plain, why = runner.parse_grant(grant(path, broad=False))
    assert plain is None and "too broad" in why
    plain, why = runner.parse_grant({k: v for k, v in grant(path).items() if k != "broad"})
    assert plain is None and "too broad" in why


@pytest.mark.parametrize("path", ["/", "/root", "/home", "/Users", "/var", "/tmp", "/etc", "/usr/share", "/var/lib/docker"])
def test_some_folders_are_refused_even_with_the_flag(path):
    g, why = runner.parse_grant(grant(path))
    assert g is None and why


@pytest.mark.parametrize("change,want", [
    ({"mode": "write"}, "read-only"), ({"purpose": "knowledge"}, "purpose use"), ({"expires": None}, "within 24 hours"),
    ({"expires": when(48)}, "within 24 hours"), ({"expires": "soon"}, "bad expiry"),
])
def test_a_broad_grant_must_be_read_only_for_use_and_short(change, want):
    g, why = runner.parse_grant(grant(**change))
    assert g is None and want in why


def test_the_flag_on_a_normal_folder_is_refused_and_normal_grants_are_not_broad():
    g, why = runner.parse_grant(grant("/data/reports"))
    assert g is None and "not a folder that can be granted as a broad folder" in why
    g, why = runner.parse_grant({"name": "r", "path": "/data/reports", "mode": "read"})
    assert why is None and g["broad"] is False


def test_an_expired_broad_grant_parses_but_is_not_active(monkeypatch, tmp_path):
    f = tmp_path / "grants.json"
    f.write_text(json.dumps({"grants": [grant(expires=when(-1)), dict(grant("/srv"), name="srv2")]}))
    monkeypatch.setattr(runner, "GRANTS_FILE", str(f))
    assert [g["name"] for g in runner.load_grants()] == ["tmp-home", "srv2"]
    assert [g["name"] for g in runner.active_grants()] == ["srv2"]


def test_the_grant_path_check_is_unchanged_without_the_flag():
    for p in ("/home/ann", "/opt", "/"):
        assert runner.grant_path_problem(p) == "that folder is too broad"
    assert runner.grant_path_problem("/home/ann", allow_broad=True) is None
    assert runner.grant_path_problem("/root", allow_broad=True) == "that folder is too broad"
    assert runner.grant_path_problem("/home/ann/.ssh", allow_broad=True) is not None


# ---------------- the spec ----------------

def test_only_a_broad_grant_gets_the_strict_flag_and_the_mount_stays_read_only():
    g, _ = runner.parse_grant(grant())
    spec = runner.build_file_spec(g, "list", "")
    assert "BROAD=1" in spec["Env"] and [m["ReadOnly"] for m in spec["HostConfig"]["Mounts"]] == [True]
    n, _ = runner.parse_grant({"name": "r", "path": "/data/reports", "mode": "read"})
    assert not [e for e in runner.build_file_spec(n, "list", "")["Env"] if e.startswith("BROAD")]


# ---------------- the reader, run for real ----------------

@pytest.fixture
def home(tmp_path):
    h = tmp_path / "ann"
    (h / "docs").mkdir(parents=True)
    (h / ".config").mkdir()
    (h / ".config" / "x.txt").write_text("config\n")
    (h / ".bash_history").write_text("history\n")
    (h / "notes.txt").write_text("hello\n")
    (h / "docs" / "plan.txt").write_text("plan\n")
    (h / "my password list.txt").write_text("pw\n")
    (h / "owner_key.txt").write_text("rlk_x\n")
    (h / "api_token.json").write_text("{}")
    (h / "Library").mkdir()
    (h / "Library" / "a.txt").write_text("mac\n")
    (h / "docs" / "wallet.dat").write_text("w")
    os.symlink(str(h / "owner_key.txt"), str(h / "innocent.txt"))
    return h


def run(base, op, rel, broad):
    env = dict(os.environ, RL_DATA=str(base), OP=op, REL=rel, MAXB="12000", MAXN="100")
    if broad:
        env["BROAD"] = "1"
    p = subprocess.run([sys.executable, "-I", "-B", "-c", runner.FILE_READER], env=env, capture_output=True, text=True, timeout=30)
    return p.returncode, json.loads(p.stdout)


def test_a_broad_listing_hides_dot_names_and_secret_looking_names(home):
    rc, d = run(home, "list", "", True)
    names = sorted(e["name"] for e in d["entries"])
    assert names == ["docs", "notes.txt"]
    rc, d = run(home, "list", "docs", True)
    assert [e["name"] for e in d["entries"]] == ["plan.txt"]


def test_a_normal_listing_is_unchanged(home):
    rc, d = run(home, "list", "", False)
    names = [e["name"] for e in d["entries"]]
    assert ".config" in names and ".bash_history" in names and "owner_key.txt" in names and "Library" in names


@pytest.mark.parametrize("rel", [".bash_history", ".config/x.txt", "my password list.txt", "owner_key.txt", "api_token.json",
                                 "Library/a.txt", "docs/wallet.dat", "innocent.txt"])
def test_a_broad_read_refuses_them(home, rel):
    rc, d = run(home, "read", rel, True)
    assert rc != 0 and "error" in d and "text" not in d, (rel, d)


def test_a_broad_read_still_reads_normal_files_and_a_normal_grant_reads_the_same_files(home):
    for rel, want in (("notes.txt", "hello\n"), ("docs/plan.txt", "plan\n")):
        rc, d = run(home, "read", rel, True)
        assert rc == 0 and d["text"] == want
    rc, d = run(home, "read", ".bash_history", False)
    assert rc == 0 and d["text"] == "history\n"


# ---------------- the host command ----------------

@pytest.fixture
def host(tmp_path):
    h = tmp_path / "home"
    (h / "grants").mkdir(parents=True)
    return h


def cli(host, *args):
    p = subprocess.run([sys.executable, str(HELPER), *args], env=dict(os.environ, RAGLEAP_HOME=str(host)),
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def grants(host):
    return json.loads((host / "grants" / "grants.json").read_text())["grants"]


needs_srv = pytest.mark.skipif(not os.path.isdir("/srv"), reason="no /srv on this machine")


@needs_srv
def test_add_broad_writes_a_short_read_only_grant_the_runner_accepts(host):
    rc, out, err = cli(host, "add", "/srv", "--name", "srvtmp", "--broad", "--for", "2h")
    assert rc == 0, err
    g = grants(host)[0]
    assert g["broad"] is True and g["mode"] == "read" and g["purpose"] == "use" and g["path"] == os.path.realpath("/srv")
    left = datetime.fromisoformat(g["expires"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=110) < left <= timedelta(hours=2)
    assert "BROAD folder" in out and "remove srvtmp" in out and "name check" in out and "guarantee" in out
    parsed, why = runner.parse_grant(g)
    assert why is None and parsed["broad"] is True
    assert "broad" in cli(host, "list")[1]


@needs_srv
def test_a_broad_folder_needs_the_flag_and_a_time_limit(host):
    rc, out, err = cli(host, "add", "/srv", "--name", "srvtmp")
    assert rc == 1 and "--broad --for 2h" in err and not (host / "grants" / "grants.json").exists()
    rc, out, err = cli(host, "add", "/srv", "--name", "srvtmp", "--broad")
    assert rc == 1 and "time limit" in err
    rc, out, err = cli(host, "add", "/srv", "--name", "srvtmp", "--broad", "--expires", "2h")
    assert rc == 1 and "--for" in err
    rc, out, err = cli(host, "add", "/srv", "--name", "srvtmp", "--broad", "--for", "2h", "--purpose", "knowledge")
    assert rc == 1 and "purpose use" in err
    rc, out, err = cli(host, "add", "/srv", "--name", "srvtmp", "--broad", "--for", "2h", "--mode", "write")
    assert rc == 1 and "read-only" in err
    assert not (host / "grants" / "grants.json").exists()


@needs_srv
@pytest.mark.parametrize("bad", ["0m", "25h", "1441m", "2d", "abc", "2"])
def test_the_time_limit_is_bounded(host, bad):
    rc, out, err = cli(host, "add", "/srv", "--name", "srvtmp", "--broad", "--for", bad)
    assert rc == 1 and "between 1m and 24h" in err and not (host / "grants" / "grants.json").exists()


@pytest.mark.parametrize("path", ["/", "/root", "/home", "/var", "/tmp", "/etc"])
def test_some_folders_stay_refused_with_the_flag(host, path):
    rc, out, err = cli(host, "add", path, "--name", "x", "--broad", "--for", "1h")
    assert rc == 1 and "Not granted" in err and not (host / "grants" / "grants.json").exists()


def test_the_flag_on_a_normal_folder_is_refused(host, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    rc, out, err = cli(host, "add", str(d), "--name", "x", "--broad", "--for", "1h")
    assert rc == 1 and "does not need --broad" in err and not (host / "grants" / "grants.json").exists()


def test_the_normal_flow_is_unchanged(host, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    rc, out, err = cli(host, "add", str(d), "--name", "plain", "--expires", "7d")
    assert rc == 0 and "broad" not in grants(host)[0] and "BROAD" not in out
    rc, out, err = cli(host, "add", "/opt", "--name", "opt")
    assert rc == 1 and "too broad" in err and "--broad --for 2h" in err


def test_the_docs_describe_broad_folders():
    text = (ROOT / "docs" / "file-access.md").read_text()
    assert "--broad --for 2h" in text and "{{" not in text and "{%" not in text
    assert "Not included yet: broad folders" not in text
