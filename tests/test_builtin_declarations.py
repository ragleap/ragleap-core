"""Declarations for the core's built-in tools (Phase 0, P0-E and P0-G): they must conform and must match the real gate."""
import ast
import json
import pathlib

import pytest

from core import autonomy, subagents
from core.agent_loop import OUTBOUND_TOOLS, TAINTING_TOOLS
from core.connector_host import builtin_declarations as bd
from core.connector_host.policy import BUILTIN_EFFECT, BUILTIN_EGRESS, GRANT_REQUIRED_OPERATIONS, OwnerPolicy, resolve_policy
from core.connector_host.registry import DeclarationRegistry, RegistryError
from core.connector_host.schema import SCHEMA_V1, check_declaration
from core.employees.actions import ACTION_TOOLS

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLISHED = json.loads((ROOT / "docs" / "capability-schema-v1.schema.json").read_text(encoding="utf-8"))
ALL = bd.builtin_declarations()
BY_TOOL = {d["operations"][0]["name"]: d for d in ALL}
EVERY_TOOL = set(ACTION_TOOLS) | {"delegate"}      # the 17 tools of the gate and the one the agent loop handles itself


def test_there_is_one_declaration_for_every_executable_tool_and_nothing_else():
    assert set(BY_TOOL) == EVERY_TOOL
    assert len(ALL) == len(EVERY_TOOL) == 18
    assert all(len(d["operations"]) == 1 for d in ALL)
    assert len({d["id"] for d in ALL}) == 18


@pytest.mark.parametrize("tool", sorted(EVERY_TOOL))
def test_every_declaration_conforms_to_both_copies_of_the_schema(tool):
    for schema in (SCHEMA_V1, PUBLISHED):
        errors = [f for f in check_declaration(BY_TOOL[tool], schema) if f[0] == "error"]
        assert errors == [], errors


@pytest.mark.parametrize("tool", sorted(EVERY_TOOL))
def test_the_effective_policy_equals_what_the_gate_applies(tool):
    d = BY_TOOL[tool]
    p = resolve_policy(d, tool, OwnerPolicy(granted_operations=frozenset({tool})))
    assert p.allowed and p.effect == BUILTIN_EFFECT[tool]
    assert "effect_raised_to_core" not in p.notes and "egress_raised_to_core" not in p.notes
    assert (p.approval == "always") == (tool in autonomy._FORCED_ACTION_TYPES)
    expected = {"tainting": tool in TAINTING_TOOLS, "outbound": tool in OUTBOUND_TOOLS}
    got = {"none": set(), "tainting": {"tainting"}, "outbound": {"outbound"}, "both": {"tainting", "outbound"}}[p.taint]
    assert got == {k for k, v in expected.items() if v}
    assert d["operations"][0].get("taint", "none") == p.taint     # the declaration states the same taint itself
    assert p.egress == BUILTIN_EGRESS.get(tool, "none")


def test_the_always_approval_tools_are_exactly_the_forced_ones():
    asked = {t for t, d in BY_TOOL.items() if d["operations"][0].get("approval") == "always"}
    assert asked == set(autonomy._FORCED_ACTION_TYPES)


def test_egress_classes_and_no_invented_hosts():
    for tool, d in BY_TOOL.items():
        net = d.get("network", {})
        assert "hosts" not in net and "tcp" not in net
        if BUILTIN_EFFECT[tool] == "external_send":
            assert net == {"egress": "owner_configured"}, tool
    assert BY_TOOL["fetch_page"]["network"] == {"egress": "runtime_chosen"}
    assert {t for t, d in BY_TOOL.items() if "network" not in d} == {
        "create_task", "run_code", "run_shell", "list_files", "read_file", "preview_change",
        "write_file", "delete_file", "request_access", "delegate"}


def test_no_declaration_names_secrets_or_budgets_or_data_classes():
    for d in ALL:
        assert "secrets" not in d and "budgets" not in d and "data_classes" not in d["descriptive"]


def test_mcp_call_is_the_only_mcp_kind():
    assert {t for t, d in BY_TOOL.items() if d["kind"] == "mcp"} == {"mcp_call"}


def test_the_text_makes_no_security_claims():
    banned = ("secure", "safe", "certified", "compliant", "production-ready")
    for d in ALL:
        text = " ".join(d["descriptive"].values()).lower()
        assert not any(w in text for w in banned), d["id"]


def test_generation_is_deterministic_and_returns_independent_copies():
    a, b = bd.builtin_declarations(), bd.builtin_declarations()
    assert a == b
    a[0]["operations"][0]["effect"] = "read"
    a[0]["network"] = {"hosts": ["x.example.com"]}
    next(d for d in a if d["id"].endswith("delegate"))["operations"][0]["inherits"]["max_depth"] = 3
    assert bd.builtin_declarations() == b


def test_unknown_tool_is_refused():
    with pytest.raises(KeyError):
        bd.builtin_declaration("no_such_tool")


def test_all_register_in_one_registry_then_it_can_be_sealed():
    reg = DeclarationRegistry()
    regs = bd.register_builtins(reg)
    assert len(regs) == 18 and len(reg.ids()) == 18
    assert bd.register_builtins(reg)[0].hash == regs[0].hash      # same content: no-op
    reg.seal()
    with pytest.raises(RegistryError):
        reg.register(bd.builtin_declaration("create_task"))


def test_a_changed_built_in_declaration_is_rejected_by_the_registry():
    reg = DeclarationRegistry()
    bd.register_builtins(reg)
    d = bd.builtin_declaration("send_webhook")
    d["descriptive"]["notes"] = "changed"
    with pytest.raises(RegistryError) as e:
        reg.register(d)
    assert e.value.code == "declaration_conflict"


def test_the_module_is_not_imported_by_the_product_yet():
    hits = []
    for p in list((ROOT / "core").rglob("*.py")) + list((ROOT / "channels").rglob("*.py")):
        if "connector_host" in p.parts:
            continue
        if "builtin_declarations" in p.read_text(encoding="utf-8", errors="ignore"):
            hits.append(str(p))
    assert hits == []


# ---------------------------------------------------------------- delegate (P0-G)

def test_delegate_must_be_granted_explicitly_and_the_core_floor_says_so():
    assert GRANT_REQUIRED_OPERATIONS == {"delegate"}
    assert BY_TOOL["delegate"]["operations"][0]["grant"] == "explicit"
    assert [t for t, d in BY_TOOL.items() if d["operations"][0].get("grant")] == ["delegate"]


def test_the_delegate_limits_equal_the_real_sub_agent_limits(monkeypatch):
    for name in ("SUBAGENT_MAX_CHILDREN", "SUBAGENT_TREE_TOKENS", "SUBAGENT_CHILD_TOKENS", "SUBAGENTS_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    inh = BY_TOOL["delegate"]["operations"][0]["inherits"]
    assert inh["max_depth"] == subagents.MAX_DEPTH
    assert inh["max_children"] == subagents.max_children()
    assert inh["child_tokens"] == subagents.child_tokens()
    assert inh["tree_tokens"] == subagents.tree_tokens()
    assert inh["tools"] == "parent_subset" and inh["taint"] == "both_ways"
    assert subagents.enabled() is False                           # still off unless the owner turns it on


def test_a_child_inherits_the_parent_taint_and_hands_its_own_back():
    src = (ROOT / "core" / "subagents.py").read_text(encoding="utf-8")
    assert '"tainted": bool(parent["tainted"])' in src            # parent -> child
    assert 'if child and child.get("tainted"):' in src and 'parent["tainted"] = True' in src   # child -> parent


def _call_sites(name):
    calls = []
    for p in list((ROOT / "core").rglob("*.py")) + list((ROOT / "channels").rglob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == name:
                calls.append(str(p.relative_to(ROOT)))
    return calls


def test_delegate_is_started_from_exactly_one_place():
    # P1-C (deliberate change): the loop calls the GATE function; only the gate calls the function that starts a child.
    assert _call_sites("execute_delegation") == ["core/agent_loop.py"]
    assert _call_sites("run_delegation") == ["core/autonomy.py"]
    assert _call_sites("delegate_step") == []
