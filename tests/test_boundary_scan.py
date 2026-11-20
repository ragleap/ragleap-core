"""Boundary tripwire (Phase 0): executors only in core/autonomy.py, connector code only through HostServices."""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest

import ragleap_boundary_scan as scan

BASELINE = json.loads((ROOT / "tests" / "data" / "integration_egress_baseline.json").read_text(encoding="utf-8"))


# ---------- the real repository ----------

def test_no_module_outside_the_gate_references_an_executor():
    assert scan.executor_violations(ROOT) == []


def test_connector_code_has_no_new_network_environment_or_subprocess_use():
    assert scan.check_connectors(ROOT, BASELINE) == []


def test_the_baseline_only_lists_real_legacy_files_and_can_only_shrink():
    rep, _ = scan.connector_report(ROOT)
    for f in list(BASELINE["legacy_connectors"]) + list(BASELINE["host_side"]):
        assert (ROOT / "core" / "integrations" / f).exists(), f"baseline names a missing file: {f}"
    # every baseline entry is still needed (check_connectors reports a stale one), shown here on a shrunk copy
    shrunk = json.loads(json.dumps(BASELINE))
    shrunk["legacy_connectors"].pop("rest_api.py")
    assert any("rest_api.py" in p for p in scan.check_connectors(ROOT, shrunk))
    grown = json.loads(json.dumps(BASELINE))
    grown["legacy_connectors"]["csv_connector.py"] = ["requests"]
    assert any("no longer needed" in p and "csv_connector.py" in p for p in scan.check_connectors(ROOT, grown))


def test_the_command_line_exits_zero_on_a_clean_tree_and_one_on_a_violation(tmp_path, capsys):
    assert scan.main(["--root", str(ROOT)]) == 0
    root = tmp_path
    (root / "core" / "integrations").mkdir(parents=True)
    (root / "core" / "integrations" / "evil.py").write_text("import requests\n")
    for d in ("channels", "scripts", "sandbox"):
        (root / d).mkdir()
    (root / "tests" / "data").mkdir(parents=True)
    (root / "tests" / "data" / "integration_egress_baseline.json").write_text(json.dumps({"legacy_connectors": {}, "host_side": {}}))
    assert scan.main(["--root", str(root)]) == 1
    assert "evil.py" in capsys.readouterr().out


# ---------- scanner self-tests: it catches each forbidden pattern and ignores comments and strings ----------

@pytest.mark.parametrize("src,expected", [
    ("import requests\n", "requests"),
    ("import requests.auth\n", "requests"),
    ("from requests import get\n", "requests"),
    ("from requests.auth import HTTPBasicAuth\n", "requests"),
    ("import httpx\n", "httpx"),
    ("import aiohttp\n", "aiohttp"),
    ("import socket\n", "socket"),
    ("import subprocess\n", "subprocess"),
    ("import urllib.request\n", "urllib.request"),
    ("from urllib import request\n", "urllib.request"),
    ("from urllib.request import urlopen\n", "urllib.request"),
    ("import http.client\n", "http.client"),
    ("from http import client\n", "http.client"),
    ("import os\nx = os.environ['A']\n", "os.environ:A"),
    ("import os\nx = os.environ.get('B')\n", "os.environ:B"),
    ("import os\nx = os.getenv('C')\n", "os.environ:C"),
    ("import os\nx = os.environ[name]\n", "os.environ:<dynamic>"),
    ("import os\nx = os.getenv(name)\n", "os.environ:<dynamic>"),
    ("from os import environ\n", "os.environ:<imported name>"),
    ("from os import getenv\n", "os.environ:<imported name>"),
    ("x = __import__('socket')\n", "dynamic-import"),
    ("import importlib\nx = importlib.import_module('requests')\n", "dynamic-import"),
    ("from importlib import import_module\n", "dynamic-import"),
])
def test_the_scanner_catches_each_forbidden_pattern(src, expected):
    viol, _ = scan.scan_connector(src)
    assert expected in viol, viol


def test_the_scanner_ignores_comments_and_strings_and_harmless_code():
    src = '''
# import requests
"""import socket; os.environ["X"]"""
text = "requests and socket and os.environ"
import json
from typing import Dict
def f():
    return json.dumps({"a": 1})
'''
    assert scan.scan_connector(src) == ([], [])


def test_database_drivers_are_reported_on_a_watch_list_but_do_not_fail():
    viol, watch = scan.scan_connector("import psycopg2\nfrom pymongo import MongoClient\n")
    assert viol == [] and watch == ["psycopg2", "pymongo"]


# ---------- executor rule self-tests ----------

def _refs(src, rel):
    return scan.scan_executors(src, rel)


def test_an_executor_referenced_in_another_module_is_found():
    src = "from core import code_exec\ndef go():\n    return code_exec.run_code('x')\n"
    assert [(r[2], r[3]) for r in _refs(src, "core/evil.py")] == [("code_exec", "run_code")]
    src2 = "from core.action_senders import send_slack\ndef go():\n    send_slack('t', 'c')\n"
    assert [(r[2], r[3]) for r in _refs(src2, "core/evil.py")] == [("action_senders", "send_slack")]
    src3 = "from core import mcp_client as m\nm.call_tool('a', 'b')\n"
    assert [(r[2], r[3]) for r in _refs(src3, "core/evil.py")] == [("mcp_client", "call_tool")]


def test_configuration_helpers_are_not_executors():
    src = "from core import code_exec, file_access\nok = code_exec.enabled() and file_access.write_enabled()\n"
    assert _refs(src, "core/employees/actions.py") == []


def test_inside_the_gate_the_senders_may_only_appear_in_send_via_channel(tmp_path):
    (tmp_path / "core").mkdir()
    for d in ("channels", "scripts", "sandbox"):
        (tmp_path / d).mkdir()
    (tmp_path / "core" / "autonomy.py").write_text(
        "from core import code_exec\n"
        "def _send_via_channel():\n    return code_exec.run_code('x')\n"
        "def other():\n    return code_exec.run_code('y')\n")
    out = scan.executor_violations(tmp_path)
    assert len(out) == 1 and "outside _send_via_channel" in out[0] and "other" in out[0]


def test_the_named_exception_is_explicit_and_reasoned():
    assert ("core/folder_ingest.py", "file_access", "_base") in scan.EXECUTOR_EXCEPTIONS
    assert all(isinstance(v, str) and len(v) > 10 for v in scan.EXECUTOR_EXCEPTIONS.values())
