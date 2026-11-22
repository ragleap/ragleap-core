"""Declarations for the core's 17 built-in tools (Phase 0, P0-E): they must conform and must match the real gate."""
import copy
import json
import pathlib

import pytest

from core import autonomy
from core.agent_loop import OUTBOUND_TOOLS, TAINTING_TOOLS
from core.connector_host import builtin_declarations as bd
from core.connector_host.policy import BUILTIN_EFFECT, resolve_policy
from core.connector_host.registry import DeclarationRegistry, RegistryError
from core.connector_host.schema import SCHEMA_V1, check_declaration
from core.employees.actions import ACTION_TOOLS

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLISHED = json.loads((ROOT / "docs" / "capability-schema-v1.schema.json").read_text(encoding="utf-8"))
ALL = bd.builtin_declarations()
BY_TOOL = {d["operations"][0]["name"]: d for d in ALL}


def test_there_is_one_declaration_for_every_built_in_tool_and_nothing_else():
    assert set(BY_TOOL) == set(ACTION_TOOLS)
    assert len(ALL) == len(ACTION_TOOLS) == 17
    assert all(len(d["operations"]) == 1 for d in ALL)
    assert len({d["id"] for d in ALL}) == 17


def test_delegate_is_not_declared_here():
    assert "delegate" not in BY_TOOL and "ragleap.builtin.delegate" not in {d["id"] for d in ALL}


@pytest.mark.parametrize("tool", sorted(ACTION_TOOLS))
def test_every_declaration_conforms_to_both_copies_of_the_schema(tool):
    for schema in (SCHEMA_V1, PUBLISHED):
        errors = [f for f in check_declaration(BY_TOOL[tool], schema) if f[0] == "error"]
        assert errors == [], errors


@pytest.mark.parametrize("tool", sorted(ACTION_TOOLS))
def test_the_effective_policy_equals_what_the_gate_applies(tool):
    d = BY_TOOL[tool]
    p = resolve_policy(d, tool)
    assert p.allowed and p.effect == BUILTIN_EFFECT[tool]
    assert "effect_raised_to_core" not in p.notes
    assert (p.approval == "always") == (tool in autonomy._FORCED_ACTION_TYPES)
    expected = {"tainting": tool in TAINTING_TOOLS, "outbound": tool in OUTBOUND_TOOLS}
    got = {"none": set(), "tainting": {"tainting"}, "outbound": {"outbound"}, "both": {"tainting", "outbound"}}[p.taint]
    assert got == {k for k, v in expected.items() if v}
    # the declaration states the same taint on its own (not only through the core floor)
    assert d["operations"][0].get("taint", "none") == p.taint


def test_the_always_approval_tools_are_exactly_the_forced_ones():
    asked = {t for t, d in BY_TOOL.items() if d["operations"][0].get("approval") == "always"}
    assert asked == set(autonomy._FORCED_ACTION_TYPES)


def test_owner_configured_targets_and_no_invented_hosts():
    for tool, d in BY_TOOL.items():
        net = d.get("network", {})
        assert "hosts" not in net and "tcp" not in net
        sends = BUILTIN_EFFECT[tool] == "external_send"
        if sends:
            assert net == {"owner_configured": True}, tool
        else:
            assert tool in bd.OWNER_CONFIGURED or "network" not in d
    assert {t for t in BY_TOOL if BUILTIN_EFFECT[t] == "external_send"} <= bd.OWNER_CONFIGURED


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
    assert bd.builtin_declarations() == b


def test_unknown_tool_is_refused():
    with pytest.raises(KeyError):
        bd.builtin_declaration("delegate")


def test_all_register_in_one_registry_then_it_can_be_sealed():
    reg = DeclarationRegistry()
    regs = bd.register_builtins(reg)
    assert len(regs) == 17 and len(reg.ids()) == 17
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
