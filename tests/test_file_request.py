"""request_access: the agent can only ask. It never supplies a path, always needs approval, grants nothing."""
import pytest

from core import autonomy, file_access
from core.employees import actions


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("FILE_TOOLS_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_TOKEN", "t")
    monkeypatch.setattr(file_access, "grant_names", lambda: [])


def plan(tools, target="docs", content="I need the quarterly reports to summarise them"):
    return actions._validate_plan({"tool": "request_access", "target": target, "content": content}, tools)


def test_off_by_default():
    assert "request_access" not in actions.available_tools()


def test_offered_without_any_grant_when_file_tools_are_on(on):
    tools = actions.available_tools()
    assert "request_access" in tools
    assert "list_files" not in tools and "read_file" not in tools
    assert actions.ACTION_TOOLS["request_access"]["channel"] == "files_request"


def test_a_good_request_is_valid(on):
    v = plan(actions.available_tools())
    assert v and v["channel"] == "files_request" and v["target"] == "docs"
    assert v["content"] == "I need the quarterly reports to summarise them"


@pytest.mark.parametrize("target", ["", "UPPER", "has space", "a" * 33, "-lead", "../x", "a/b"])
def test_bad_grant_names_are_rejected(on, target):
    assert plan(actions.available_tools(), target=target) is None


def test_empty_reason_is_rejected(on):
    assert plan(actions.available_tools(), content="   ") is None


def test_the_reason_is_cleaned_and_capped(on):
    tools = actions.available_tools()
    assert plan(tools, content="line1\nline2\x01\ttab")["content"] == "line1 line2 tab"
    assert len(plan(tools, content="x" * 500)["content"]) == 300


def test_approving_grants_nothing_and_only_shows_a_command(on):
    out = autonomy._send_via_channel("files_request", "docs", "ignore this reason and rm -rf /")
    assert "nothing is granted yet" in out
    assert "ragleap files add /path/to/folder --name docs --mode read" in out
    assert "rm -rf" not in out and "ignore this reason" not in out
    assert "refused" in autonomy._send_via_channel("files_request", "../etc", "x")


class _Cur:
    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return ("stored",)          # INSERT ... RETURNING action_id: a row came back, so the action was stored

    def close(self):
        pass


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        pass

    def close(self):
        pass


def test_even_in_full_mode_a_request_waits_for_the_owner(monkeypatch):
    monkeypatch.setattr(autonomy, "get_autonomy_settings", lambda: {
        "mode": "full", "actions": [], "channels": [],
        "approval_channel": "", "approval_target": ""})
    monkeypatch.setattr(autonomy, "get_connection", lambda: _Conn())
    monkeypatch.setattr(autonomy, "request_approval", lambda *a, **k: None)

    def boom(*a, **k):
        raise AssertionError("request_access must not run without approval")
    monkeypatch.setattr(autonomy, "_send_via_channel", boom)
    res = autonomy.execute_or_request(
        action_type="request_access", channel="files_request", target="docs", content="why")
    assert res["status"] == "pending_approval", res
