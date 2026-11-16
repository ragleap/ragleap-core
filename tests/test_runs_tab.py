"""Dashboard Runs tab: sub-agent runs shown under their parent, a Cancel run button, a neutral look for cancelled runs.
The grouping and the cancel text are pure functions that are cut out of app.js and run in Node; the rest is static checks
(the page itself stays text nodes only, see test_office.py)."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "core" / "office_static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def helpers_block():
    m = re.search(r"// runsHelpers:begin\n(.*?)// runsHelpers:end", APP, re.S)
    assert m, "the helpers block is missing from app.js"
    return m.group(1)


def run_node(cases_js, tmp_path):
    module = tmp_path / "runs_helpers.js"
    module.write_text(helpers_block() + "\nmodule.exports = { groupRuns, cancelText };\n", encoding="utf-8")
    script = "const fn = require(%s);\n" % json.dumps(str(module)) + cases_js
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def r(rid, parent=None, created="2026-11-14T10:00:00", **kw):
    d = {"id": rid, "parent_id": parent, "created_at": created, "status": "done"}
    d.update(kw)
    return d


@needs_node
def test_children_are_grouped_under_their_parent_in_creation_order(tmp_path):
    runs = [r("other", created="04"), r("c2", "p", "03"), r("c1", "p", "02"), r("p", None, "01")]
    out = run_node("const g = fn.groupRuns(%s); console.log(JSON.stringify({top: g.top.map(x => x.id), "
                   "kids: Object.fromEntries([...g.kids].map(([k, v]) => [k, v.map(x => x.id)]))}));" % json.dumps(runs), tmp_path)
    assert out == {"top": ["other", "p"], "kids": {"p": ["c1", "c2"]}}


@needs_node
def test_a_run_whose_parent_is_not_in_the_list_is_shown_on_its_own_and_odd_input_is_safe(tmp_path):
    runs = [r("lonely", "missing"), r("self", "self"), r("plain")]
    out = run_node("const g = fn.groupRuns(%s); console.log(JSON.stringify({top: g.top.map(x => x.id), n: g.kids.size, "
                   "none: fn.groupRuns(null).top.length, undef: fn.groupRuns(undefined).kids.size, str: fn.groupRuns('x').top.length}));"
                   % json.dumps(runs), tmp_path)
    assert out == {"top": ["lonely", "self", "plain"], "n": 0, "none": 0, "undef": 0, "str": 0}


@needs_node
def test_the_cancel_message_says_what_happened(tmp_path):
    out = run_node("console.log(JSON.stringify([fn.cancelText({cancelled: ['a', 'b'], stopping: ['c']}), "
                   "fn.cancelText({cancelled: ['a'], stopping: []}), fn.cancelText({cancelled: [], stopping: []}), "
                   "fn.cancelText({})]));", tmp_path)
    assert out == ["Cancelled 2 run(s), 1 still stopping.", "Cancelled 1 run(s).",
                   "Nothing to cancel: the run had already finished.", "Nothing to cancel: the run had already finished."]


def test_the_cancel_button_posts_to_the_cancel_route_for_live_runs_only_and_asks_twice():
    assert 'approvalButton("Cancel run", "bad", "/agent-runs/" + encodeURIComponent(r.id) + "/cancel", msg)' in APP
    assert 'const live = r.status === "running" || r.status === "waiting_approval";' in APP
    assert "live && !r.cancel_requested" in APP
    assert "Click again to confirm" in APP                          # the shared two-click button
    assert "Array.isArray(r.cancelled) ? cancelText(r)" in APP     # the reply of the cancel route is explained


def test_the_runs_tab_asks_for_enough_runs_and_nests_children_with_a_depth_limit():
    assert '"/agent-runs?limit=60"' in APP and "g.top.map((r) => runCard(r, g.kids, 0))" in APP
    assert "(level || 0) < 4" in APP and 'h("div", { class: "children" }' in APP
    assert '"sub-agent run " + String(s.child_run_id).slice(0, 8)' in APP


def test_a_cancelled_run_is_not_shown_as_an_error_and_the_new_styles_are_plain():
    assert 'r.status === "running" || r.status === "cancelled" ? "" : "bad"' in APP
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".children {" in css and "url(" not in css
    assert "innerHTML" not in APP and "localStorage" not in APP and "style=" not in APP


def test_the_run_list_gives_the_dashboard_the_parent_fields():
    from core import agent_loop, api
    from core.employees._db import get_connection

    def save(parent, depth):
        run = {"id": None, "role": "support", "query": "TEST-TAB: " + str(depth), "context_answer": "", "status": "running",
               "steps": [], "tainted": False, "pending_action_id": None, "summary": "", "parent_id": parent, "depth": depth}
        agent_loop._save(run)
        return run["id"]
    try:
        top = save(None, 0)
        child = save(top, 1)
        got = {x["id"]: x for x in api.list_agent_runs(limit=200)["runs"]}
        assert got[child]["parent_id"] == top and got[child]["depth"] == 1 and got[top]["parent_id"] is None
        assert got[child]["cancel_requested"] is False and got[child]["role"] == "support"
    finally:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM agent_runs WHERE query LIKE 'TEST-TAB%'")
        conn.commit()
        cur.close()
        conn.close()


def test_the_docs_mention_the_dashboard_view():
    text = (STATIC.parent.parent / "docs" / "sub-agents.md").read_text(encoding="utf-8")
    assert "Cancel run button" in text and "a dashboard view of the tree" not in text and "{{" not in text
