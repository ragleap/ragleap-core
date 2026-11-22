"""Egress classes, explicit grants and bounded inheritance in the effective policy and the registry (Phase 0, P0-G).

Pure code: the gate does not call any of it yet. Phase 1 re-runs these negative tests against the gate itself.
"""
import copy

import pytest

from core.connector_host import policy
from core.connector_host.builtin_declarations import builtin_declaration
from core.connector_host.policy import (EGRESS_OBLIGATIONS, EGRESS_RANK, OwnerPolicy, declared_egress, resolve_policy)
from core.connector_host.registry import DeclarationRegistry, RegistryError

RUNTIME_OBLIGATIONS = {"deny_when_egress_disabled", "block_metadata_and_link_local", "block_non_public_before_and_after_dns",
                       "revalidate_each_redirect_hop", "audit_resolved_url_and_status"}


def decl(op, network=None, **extra):
    d = {"schema": "ragleap.capability/1", "id": "example.thing", "version": "1.0.0", "kind": "action", "operations": [op]}
    if network is not None:
        d["network"] = network
    d.update(extra)
    return d


def op(name="do_it", effect="read", **kw):
    return dict({"name": name, "effect": effect}, **kw)


# ---------------------------------------------------------------- egress classes

def test_the_three_classes_and_none_are_ordered_by_reach():
    assert EGRESS_RANK == {"none": 0, "fixed_hosts": 1, "owner_configured": 2, "runtime_chosen": 3}
    assert set(EGRESS_OBLIGATIONS) == set(EGRESS_RANK)


def test_the_class_is_inferred_when_a_declaration_does_not_state_one():
    assert declared_egress(decl(op())) == "none"
    assert declared_egress(decl(op(), {"hosts": ["api.example.com"]})) == "fixed_hosts"
    assert declared_egress(decl(op(), {"tcp": [{"host": "db.example.com", "port": 5432}]})) == "fixed_hosts"
    assert declared_egress(decl(op(), {"egress": "runtime_chosen"})) == "runtime_chosen"
    assert declared_egress(decl(op(), {"egress": ["runtime_chosen"]})) == "none"       # a malformed value is not a class
    assert declared_egress({}) == "none" and declared_egress("x") == "none"


def test_runtime_chosen_carries_every_obligation_and_no_host_list():
    p = resolve_policy(decl(op("fetch_it", "read"), {"egress": "runtime_chosen"}), "fetch_it")
    assert p.allowed and p.egress == "runtime_chosen" and p.hosts == ()
    assert set(p.egress_obligations) == RUNTIME_OBLIGATIONS


def test_the_other_classes_carry_the_base_obligations_only():
    base = {"deny_when_egress_disabled", "block_metadata_and_link_local"}
    for net in ({"egress": "owner_configured"}, {"hosts": ["api.example.com"], "egress": "fixed_hosts"}):
        p = resolve_policy(decl(op("send_it", "external_send"), net), "send_it")
        assert set(p.egress_obligations) == base, net
    p = resolve_policy(decl(op()), "do_it")
    assert p.egress == "none" and p.egress_obligations == ()


@pytest.mark.parametrize("net", [{"egress": "runtime_chosen"}, {"egress": "owner_configured"},
                                 {"hosts": ["api.example.com"], "egress": "fixed_hosts"}])
def test_every_egress_class_is_denied_when_egress_is_disabled(net):
    p = resolve_policy(decl(op("send_it", "external_send"), net), "send_it", OwnerPolicy(egress_enabled=False))
    assert not p.allowed and p.denial_code == "egress_disabled" and p.egress == net["egress"]


def test_an_operation_without_egress_still_runs_when_egress_is_disabled():
    assert resolve_policy(decl(op()), "do_it", OwnerPolicy(egress_enabled=False)).allowed


def test_egress_is_enabled_by_default():
    assert OwnerPolicy().egress_enabled is True
    assert resolve_policy(decl(op("fetch_it"), {"egress": "runtime_chosen"}), "fetch_it").allowed


def test_the_core_raises_the_class_of_its_own_tools_and_never_lowers_it():
    p = resolve_policy(decl(op("fetch_page", "read")), "fetch_page")          # declares nothing, the core knows better
    assert p.egress == "runtime_chosen" and "egress_raised_to_core" in p.notes
    assert set(p.egress_obligations) == RUNTIME_OBLIGATIONS
    p = resolve_policy(decl(op("send_email", "external_send"), {"hosts": ["mail.example.com"]}), "send_email")
    assert p.egress == "owner_configured" and "egress_raised_to_core" in p.notes


@pytest.mark.parametrize("tool,net,code", [
    ("fetch_page", None, "egress_weaker_than_core"),      # a tool that reaches the web declared with no egress at all
    ("send_webhook", None, "invalid_declaration")])       # an external_send without any network fact fails the checker first
def test_the_registry_refuses_a_built_in_tool_declared_with_no_egress(tool, net, code):
    d = builtin_declaration(tool)
    d.pop("network", None)
    with pytest.raises(RegistryError) as e:
        DeclarationRegistry().register(d)
    assert e.value.code == code


@pytest.mark.parametrize("tool,net,expected", [("fetch_page", {"egress": "owner_configured"}, "runtime_chosen"),
                                               ("fetch_page", {"hosts": ["a.example.com"]}, "runtime_chosen"),
                                               ("send_slack", {"hosts": ["a.example.com"]}, "owner_configured")])
def test_a_narrower_claim_for_a_built_in_tool_is_accepted_but_the_resolver_keeps_the_core_class(tool, net, expected):
    d = builtin_declaration(tool)
    d["network"] = net
    DeclarationRegistry().register(d)
    p = resolve_policy(d, tool)
    assert p.egress == expected and "egress_raised_to_core" in p.notes


def test_the_audit_record_names_the_class_and_the_obligations_and_no_arguments():
    a = resolve_policy(decl(op("fetch_page", "read")), "fetch_page").to_audit()
    assert a["egress"] == "runtime_chosen" and set(a["egress_obligations"]) == RUNTIME_OBLIGATIONS
    assert a["policy_hash"] == resolve_policy(decl(op("fetch_page", "read")), "fetch_page").to_audit()["policy_hash"]
    changed = resolve_policy(decl(op("fetch_page", "read")), "fetch_page", OwnerPolicy(egress_enabled=False)).to_audit()
    assert changed["policy_hash"] != a["policy_hash"] and changed["denial_code"] == "egress_disabled"
    assert "args" not in a and "arguments" not in a


# ---------------------------------------------------------------- explicit grants and bounded inheritance

def test_delegate_is_denied_unless_the_owner_grants_it():
    d = builtin_declaration("delegate")
    assert resolve_policy(d, "delegate", OwnerPolicy()).denial_code == "operation_not_granted"
    assert resolve_policy(d, "delegate", OwnerPolicy(granted_operations=frozenset({"create_task"}))).denial_code == "operation_not_granted"
    p = resolve_policy(d, "delegate", OwnerPolicy(granted_operations=frozenset({"delegate"})))
    assert p.allowed and p.effect == "execute" and p.approval == "per_mode"


def test_the_grant_is_off_by_default_and_nothing_is_granted_implicitly():
    assert OwnerPolicy().granted_operations == frozenset()
    assert not resolve_policy(builtin_declaration("delegate"), "delegate").allowed


def test_a_declaration_cannot_remove_the_grant_requirement_of_the_core():
    d = decl(op("delegate", "execute"))                                         # no grant field at all
    assert resolve_policy(d, "delegate").denial_code == "operation_not_granted"
    reg = DeclarationRegistry()
    with pytest.raises(RegistryError) as e:
        reg.register(d)
    assert e.value.code == "grant_weaker_than_core"


def test_any_operation_can_ask_for_an_explicit_grant():
    d = decl(op("big_job", "execute", grant="explicit"))
    assert resolve_policy(d, "big_job").denial_code == "operation_not_granted"
    assert resolve_policy(d, "big_job", OwnerPolicy(granted_operations=frozenset({"big_job"}))).allowed


def test_the_order_of_the_denials_is_autonomy_then_owner_list_then_grant_then_egress():
    d = decl(op("big_job", "external_send", grant="explicit"), {"egress": "owner_configured"})
    granted = frozenset({"big_job"})
    assert resolve_policy(d, "big_job", OwnerPolicy(mode="off", granted_operations=granted, egress_enabled=False)).denial_code == "autonomy_off"
    assert resolve_policy(d, "big_job", OwnerPolicy(allowed_operations=frozenset(), granted_operations=granted, egress_enabled=False)).denial_code == "operation_not_allowed_by_owner"
    assert resolve_policy(d, "big_job", OwnerPolicy(egress_enabled=False)).denial_code == "operation_not_granted"
    assert resolve_policy(d, "big_job", OwnerPolicy(granted_operations=granted, egress_enabled=False)).denial_code == "egress_disabled"
    assert resolve_policy(d, "big_job", OwnerPolicy(granted_operations=granted)).allowed


def test_the_inheritance_bounds_are_recorded_in_the_effective_policy_and_the_audit():
    p = resolve_policy(builtin_declaration("delegate"), "delegate", OwnerPolicy(granted_operations=frozenset({"delegate"})))
    assert p.inherits["max_depth"] == 1 and p.inherits["max_children"] == 3 and p.inherits["tools"] == "parent_subset"
    assert p.to_audit()["inherits"] == dict(sorted(p.inherits.items()))
    assert resolve_policy(decl(op()), "do_it").inherits == {}


def test_the_returned_inheritance_is_a_copy():
    d = builtin_declaration("delegate")
    p = resolve_policy(d, "delegate", OwnerPolicy(granted_operations=frozenset({"delegate"})))
    p.inherits["max_depth"] = 99
    assert d["operations"][0]["inherits"]["max_depth"] == 1


def test_the_floor_tables_are_the_ones_the_docs_describe():
    assert policy.GRANT_REQUIRED_OPERATIONS == {"delegate"}
    assert policy.BUILTIN_EFFECT["delegate"] == "execute"
    assert policy.BUILTIN_EGRESS["fetch_page"] == "runtime_chosen"
    assert set(policy.BUILTIN_EGRESS) == {"send_webhook", "send_slack", "send_email", "update_crm_record", "schedule_event",
                                          "notify_owner", "mcp_call", "fetch_page"}
