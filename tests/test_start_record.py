"""P1-F: the signed start record of a child binds what it could DO (handler identity and effect class per tool), what it was TOLD
(role and instruction text) and the taint it started with. These tests need no database."""
import ast
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import autonomy, subagents
from core.employees import actions

ROOT = Path(__file__).resolve().parent.parent
T = {"run_code": "x", "fetch_page": "y"}


def _dispatcher_branches():
    """channel -> set of 'module:function' the _send_via_channel branch for that channel calls, read from its source."""
    tree = ast.parse((ROOT / "core" / "autonomy.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_send_via_channel")
    out = {}
    for node in ast.walk(fn):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "channel" and isinstance(node.test.comparators[0], ast.Constant)):
            continue
        direct, alias, found = {}, {}, set()
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.ImportFrom):
                    for a in sub.names:
                        direct[a.asname or a.name] = f"{sub.module}:{a.name}"
                        alias[a.asname or a.name] = f"{sub.module}.{a.name}"
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name) and f.id in direct:
                        found.add(direct[f.id])
                    elif isinstance(f, ast.Name) and f.id == "_send_via_channel":
                        found.add("core.autonomy:_send_via_channel")
                    elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in alias:
                        found.add(f"{alias[f.value.id]}:{f.attr}")
        out.setdefault(node.test.comparators[0].value, set()).update(found)
    return out


def test_the_handler_table_agrees_with_the_dispatcher_and_with_the_tool_list():
    from core.connector_host.policy import BUILTIN_EFFECT
    branches = _dispatcher_branches()
    assert len(branches) >= 15, branches.keys()
    for tool, spec in actions.ACTION_TOOLS.items():
        assert tool in autonomy.TOOL_HANDLERS, tool
        assert autonomy.TOOL_HANDLERS[tool] in branches[spec["channel"]], (tool, autonomy.TOOL_HANDLERS[tool], branches[spec["channel"]])
    assert set(autonomy.TOOL_HANDLERS) == set(actions.ACTION_TOOLS) | {"delegate"}
    assert set(BUILTIN_EFFECT) <= set(autonomy.TOOL_HANDLERS)


def test_every_handler_resolves_to_a_real_function_with_the_name_in_the_table():
    for tool, spec in autonomy.TOOL_HANDLERS.items():
        ident = autonomy._handler_identity(tool)
        assert not ident.startswith("unresolved"), (tool, ident)
        assert ident == spec, (tool, ident, spec)
    assert autonomy._handler_identity("not_a_tool") == "unresolved"
    assert autonomy.unresolved_tools({"run_code": "x", "mystery": "y"}) == ["mystery"]


def test_the_digest_is_versioned_and_built_from_name_handler_and_effect():
    entries = [{"name": "fetch_page", "handler": "core.page_fetch:fetch_page", "effect": "read"},
               {"name": "run_code", "handler": "core.code_exec:run_code", "effect": "execute"}]
    want = hashlib.sha256(json.dumps({"v": 2, "tools": entries}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert autonomy.tools_digest(T) == want


def test_names_order_and_descriptions(monkeypatch):
    d = autonomy.tools_digest(T)
    assert d == autonomy.tools_digest({"fetch_page": "other words", "run_code": "also other"})       # descriptions embed the owner's own config
    assert d == autonomy.tools_digest(dict(reversed(list(T.items()))))
    assert d != autonomy.tools_digest({"run_code": "x"}) and d != autonomy.tools_digest(dict(T, run_shell="z"))
    assert d != autonomy.tools_digest({"run_code": "x", "read_file": "y"})


def test_a_swapped_handler_or_a_changed_effect_class_changes_the_digest(monkeypatch):
    from core import code_exec
    from core.connector_host import policy
    d = autonomy.tools_digest(T)
    original = code_exec.run_code

    def wrapper(*a, **k):
        return original(*a, **k)
    monkeypatch.setattr(code_exec, "run_code", wrapper)                           # the same name, another function behind it
    assert autonomy.tools_digest(T) != d
    monkeypatch.setattr(code_exec, "run_code", original)
    assert autonomy.tools_digest(T) == d
    monkeypatch.setitem(policy.BUILTIN_EFFECT, "fetch_page", "write")             # the declared effect class
    assert autonomy.tools_digest(T) != d


def test_an_empty_list_has_a_real_digest_and_is_not_none():
    d = autonomy.tools_digest({})
    assert isinstance(d, str) and len(d) == 64 and d != autonomy.tools_digest(T) and d == autonomy.tools_digest(None)


def test_the_task_digest_binds_role_and_text_exactly():
    d = autonomy.task_digest("support", "explain a pending approval")
    assert len(d) == 64 and d == autonomy.task_digest("support", "explain a pending approval")
    assert d != autonomy.task_digest("sales", "explain a pending approval")
    assert d != autonomy.task_digest("support", "explain a pending approval ")
    assert d != autonomy.task_digest("support", "explain a pending approvaI")


def test_the_resolver_sees_only_four_fields_so_spawn_and_drive_cannot_drift(monkeypatch):
    seen = []
    monkeypatch.setattr(subagents, "with_delegate", lambda tools, run: (seen.append(run), tools)[1])
    monkeypatch.setattr(subagents, "limit_to_parent", lambda tools, run: (seen.append(run), tools)[1])
    monkeypatch.setattr(actions, "available_tools", lambda: {"run_code": "x"})
    row = {"id": "1", "parent_id": "2", "role": "support", "depth": 1, "forced": True, "tainted": True, "query": "q", "steps": [1]}
    synthetic = {"id": "1", "parent_id": "2", "role": "support", "depth": 1}
    assert subagents.resolved_tools(row) == subagents.resolved_tools(synthetic) == {"run_code": "x"}
    assert all(set(dict(v)) == {"id", "parent_id", "role", "depth"} for v in seen) and len(seen) == 4
    for view in seen[:2]:
        for bad in ("forced", "tainted", "query"):
            with pytest.raises(KeyError):
                view[bad]
            with pytest.raises(KeyError):
                view.get(bad)
        assert view["role"] == "support" and view.get("depth") == 1


def test_a_resolver_that_starts_reading_another_field_fails_loudly(monkeypatch):
    monkeypatch.setattr(actions, "available_tools", lambda: {"run_code": "x"})
    monkeypatch.setattr(subagents, "with_delegate", lambda tools, run: tools)
    monkeypatch.setattr(subagents, "limit_to_parent", lambda tools, run: {k: v for k, v in tools.items() if not run["forced"]})
    with pytest.raises(KeyError):
        subagents.resolved_tools({"id": "1", "parent_id": "2", "role": "support", "depth": 1, "forced": False})
