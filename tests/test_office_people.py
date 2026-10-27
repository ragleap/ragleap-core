"""The dashboard and people: tabs by role, the Users and Audit tabs, who approved. Text checks on the page code
(the general dashboard rules in test_office.py still apply to it)."""
import pathlib
import re

JS = (pathlib.Path(__file__).resolve().parent.parent / "core" / "office_static" / "app.js").read_text()
PEOPLE = JS[JS.index("// ---- Audit (admin)"):JS.index("const refresh = async")]


def test_every_tab_has_the_lowest_role_that_can_use_it():
    table = re.search(r"const TAB_ROLE = \{(.*?)\};", JS, re.S).group(1)
    got = dict(re.findall(r"(\w+): \"(\w+)\"", table))
    tabs = re.findall(r"\[\"(\w+)\", \"[^\"]+\"\]", re.search(r"const TABS = \[(.*?)\];", JS, re.S).group(1))
    assert sorted(got) == sorted(tabs)
    assert got["users"] == "owner" and got["audit"] == "admin" and got["settings"] == "admin"
    assert got["approvals"] == "operator" and got["overview"] == "viewer"


def test_the_page_matches_the_servers_role_table():
    from core import permissions
    need = {"overview": ("GET", "/overview"), "approvals": ("GET", "/autonomy/pending"), "tasks": ("GET", "/tasks"),
            "documents": ("GET", "/documents"), "sources": ("GET", "/integrations"), "runs": ("GET", "/agent-runs"),
            "log": ("GET", "/autonomy/log"), "settings": ("GET", "/settings"), "audit": ("GET", "/audit"),
            "users": ("GET", "/users"), "files": ("GET", "/file-grants"), "proactive": ("GET", "/proactive-triggers")}
    table = dict(re.findall(r"(\w+): \"(\w+)\"", re.search(r"const TAB_ROLE = \{(.*?)\};", JS, re.S).group(1)))
    for tab, (method, path) in need.items():
        assert table[tab] == permissions.required_role(method, path), (tab, table[tab])


def test_an_older_server_without_me_gets_no_users_or_audit_tab():
    assert 'id !== "audit" && id !== "users"' in JS
    assert 'try { me = await api("GET", "/me"); }' in JS


def test_you_cannot_lock_yourself_out_from_the_page():
    assert "me.user_id === u.id" in PEOPLE and "mine ? null :" in PEOPLE


def test_a_key_is_only_shown_when_it_was_just_made_and_is_never_stored():
    assert "newKey = { name: u.name, key: r.key };" in PEOPLE
    assert "shown only once" in PEOPLE
    assert "localStorage" not in JS and "sessionStorage.setItem" not in PEOPLE


def test_who_approved_is_shown_in_the_activity_log():
    assert '" by " + clip(r.decided_by, 80)' in JS


def test_names_and_paths_go_in_as_text():
    assert "innerHTML" not in PEOPLE and "url(" not in PEOPLE and "style=" not in PEOPLE
