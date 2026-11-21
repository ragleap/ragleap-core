"""Policy resolution, the immutable declaration registry, host-measured usage and fail-closed budgets (Phase 0, P0-D).

Everything here is pure: the gate does not call any of it yet. Phase 1 re-runs the negative tests against the gate itself.
"""
import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest

from core.connector_host import contract as c
from core.connector_host import denials, policy
from core.connector_host.contract import Usage
from core.connector_host.policy import (CORE_CEILINGS, OwnerPolicy, canonical_hash, resolve_policy)
from core.connector_host.registry import DeclarationRegistry, RegistryError
from core.connector_host.schema import SCHEMA_V1
from core.connector_host.usage import (BudgetChecker, MemoryCounterStore, StoreUnavailable, mismatched_fields,
                                       usage_mismatch_event)

BASE = {"schema": "ragleap.capability/1", "id": "example.crm", "version": "1.0.0", "kind": "action",
        "operations": [{"name": "update_note", "effect": "external_send", "approval": "per_mode", "taint": "outbound"},
                       {"name": "list_notes", "effect": "read"}],
        "network": {"hosts": ["api.example-crm.com"], "https_only": True},
        "secrets": ["EXAMPLE_CRM_TOKEN"],
        "budgets": {"max_calls_per_run": 5, "max_calls_per_day": 100, "max_bytes": 262144, "timeout_s": 20},
        "descriptive": {"data_classes": ["personal"]}}


def decl(**changes):
    d = copy.deepcopy(BASE)
    d.update(changes)
    return d


def op_decl(name, effect, **extra):
    return decl(operations=[dict({"name": name, "effect": effect}, **extra)])


# ---------------------------------------------------------------- the published schema and the embedded copy

def test_the_embedded_schema_is_identical_to_the_published_file():
    published = json.loads((ROOT / "docs" / "capability-schema-v1.schema.json").read_text(encoding="utf-8"))
    assert SCHEMA_V1 == published


# ---------------------------------------------------------------- the core's own tables match the real gate

def test_the_core_floor_tables_equal_the_real_sets():
    from core import autonomy
    from core import agent_loop
    from core.employees import actions
    assert set(policy.BUILTIN_EFFECT) == set(actions.ACTION_TOOLS)
    assert policy.OUTBOUND_BUILTINS == set(agent_loop.OUTBOUND_TOOLS)
    assert policy.TAINTING_BUILTINS == set(agent_loop.TAINTING_TOOLS)
    assert policy.ALWAYS_APPROVAL_OPERATIONS == set(autonomy._FORCED_ACTION_TYPES)
    assert set(policy.EFFECT_RANK) == {"read", "write", "execute", "external_send"}


# ---------------------------------------------------------------- negative tests: a declaration can only narrow

@pytest.mark.parametrize("name", ["write_file", "delete_file", "request_access"])
def test_file_writes_deletes_and_access_requests_always_need_approval_even_if_a_loader_lets_never_through(name):
    # the validator rejects 'never' (next test); here a buggy loader hands the value to the resolver anyway
    d = op_decl(name, "write", approval="never")
    p = resolve_policy(d, name)
    assert p.allowed and p.approval == "always"
    assert "core_floor:always_approval_operation" in p.approval_reasons and "invalid_approval_ignored" in p.notes


def test_approval_never_is_never_honoured_for_any_other_operation_either():
    p = resolve_policy(op_decl("update_note", "external_send", approval="never"), "update_note")
    assert p.approval == "per_mode" and "invalid_approval_ignored" in p.notes     # never weaker than the standard rule


def test_approval_never_does_not_register():
    reg = DeclarationRegistry()
    with pytest.raises(RegistryError) as e:
        reg.register(op_decl("write_file", "write", approval="never"))
    assert e.value.code == "invalid_declaration"


def test_a_declaration_may_ask_for_more_approval():
    p = resolve_policy(op_decl("update_note", "external_send", approval="always"), "update_note")
    assert p.approval == "always" and p.approval_reasons == ("declared",)


def test_an_undeclared_operation_is_denied():
    p = resolve_policy(BASE, "delete_everything")
    assert not p.allowed and p.denial_code == policy.D_UNDECLARED


def test_a_weaker_effect_than_the_core_table_is_rejected_at_registration():
    reg = DeclarationRegistry()
    with pytest.raises(RegistryError) as e:
        reg.register(op_decl("write_file", "read"))
    assert e.value.code == "effect_weaker_than_core"
    with pytest.raises(RegistryError):
        reg.register(op_decl("send_email", "write", ))      # the core classifies send_email as external_send
    reg.register(op_decl("send_email", "external_send"))     # the same name with the right effect is fine


def test_the_effect_is_raised_to_the_core_classification_by_the_resolver_too():
    p = resolve_policy(op_decl("send_email", "read"), "send_email")
    assert p.effect == "external_send" and "effect_raised_to_core" in p.notes


def test_the_owner_list_wins_over_a_wider_declaration():
    d = decl()
    d["network"]["hosts"] = ["api.example-crm.com", "other.example.com"]
    p = resolve_policy(d, "update_note", OwnerPolicy(allowed_hosts=frozenset({"api.example-crm.com"})))
    assert p.hosts == ("api.example-crm.com",) and p.narrowed == ("other.example.com",)


def test_an_operation_the_owner_did_not_allow_stays_denied_and_autonomy_off_denies_everything():
    assert resolve_policy(BASE, "update_note", OwnerPolicy(allowed_operations=frozenset({"list_notes"}))).denial_code == policy.D_NOT_ALLOWED_BY_OWNER
    assert resolve_policy(BASE, "list_notes", OwnerPolicy(mode="off")).denial_code == policy.D_AUTONOMY_OFF
    assert resolve_policy(BASE, "list_notes", OwnerPolicy(allowed_operations=frozenset())).denial_code == policy.D_NOT_ALLOWED_BY_OWNER


def test_taint_none_cannot_remove_the_core_taint_rule():
    p = resolve_policy(op_decl("send_email", "external_send", taint="none"), "send_email")
    assert p.taint == "outbound"
    assert resolve_policy(op_decl("fetch_page", "read", taint="none"), "fetch_page").taint == "both"
    assert resolve_policy(op_decl("read_file", "read", taint="none"), "read_file").taint == "tainting"


def test_for_an_unknown_operation_the_effect_decides_the_taint_floor():
    assert resolve_policy(op_decl("push_report", "external_send", taint="none"), "push_report").taint == "outbound"
    assert resolve_policy(op_decl("push_report", "write", taint="none"), "push_report").taint == "outbound"
    assert resolve_policy(op_decl("peek", "read", taint="none"), "peek").taint == "none"
    assert resolve_policy(op_decl("peek", "read", taint="tainting"), "peek").taint == "tainting"


def test_the_runtime_forcing_rules_are_always_listed_and_cannot_be_declared_away():
    for t in ("none", "tainting", "outbound", "both"):
        assert resolve_policy(op_decl("send_email", "external_send", taint=t), "send_email").runtime_forcing == ("tainted_run", "sensitive_role")


def test_budgets_above_the_core_ceilings_are_clamped_and_missing_ones_get_the_ceiling():
    d = decl(budgets={"max_calls_per_run": 1000, "timeout_s": 5})
    p = resolve_policy(d, "update_note")
    assert p.budgets["max_calls_per_run"] == CORE_CEILINGS["max_calls_per_run"] and "max_calls_per_run" in p.clamped
    assert p.budgets["timeout_s"] == 5 and p.budgets["max_bytes"] == CORE_CEILINGS["max_bytes"]
    d2 = copy.deepcopy(BASE)
    d2.pop("budgets")
    assert resolve_policy(d2, "update_note").budgets == dict(sorted(CORE_CEILINGS.items()))


def test_wildcard_hosts_and_an_unknown_schema_major_are_refused_at_registration():
    reg = DeclarationRegistry()
    d = decl()
    d["network"]["hosts"] = ["*.example.com"]
    with pytest.raises(RegistryError) as e:
        reg.register(d)
    assert e.value.code == "invalid_declaration"
    with pytest.raises(RegistryError) as e:
        reg.register(decl(schema="ragleap.capability/2"))
    assert e.value.code == "unsupported_schema_version"


def test_descriptive_fields_change_no_policy_decision_and_no_hash_of_the_policy():
    a = decl()
    b = decl(descriptive={"title": "x", "data_classes": ["credentials", "financial"]})
    pa, pb = resolve_policy(a, "update_note").to_audit(), resolve_policy(b, "update_note").to_audit()
    for k in ("approval", "taint", "effect", "hosts", "budgets", "allowed", "approval_reasons", "runtime_forcing", "scope"):
        assert pa[k] == pb[k]


# ---------------------------------------------------------------- the effective policy in audit events

def test_the_audit_form_has_the_declaration_identity_and_a_stable_policy_hash():
    reg = DeclarationRegistry().register(BASE)
    a = resolve_policy(reg.data, "update_note", declaration_hash=reg.hash).to_audit()
    b = resolve_policy(reg.to_dict(), "update_note", declaration_hash=reg.hash).to_audit()
    assert a == b and len(a["policy_hash"]) == 64
    assert a["declaration"] == {"id": "example.crm", "version": "1.0.0", "hash": reg.hash}
    changed = resolve_policy(reg.to_dict(), "update_note", OwnerPolicy(allowed_hosts=frozenset()), declaration_hash=reg.hash).to_audit()
    assert changed["policy_hash"] != a["policy_hash"]


def test_the_audit_form_never_contains_arguments_or_secrets():
    text = json.dumps(resolve_policy(BASE, "update_note").to_audit())
    assert "EXAMPLE_CRM_TOKEN" not in text and "secrets" not in text and '"args"' not in text


# ---------------------------------------------------------------- the registry: immutable declarations

def test_registration_returns_a_frozen_snapshot_with_a_canonical_hash():
    events = []
    reg = DeclarationRegistry(audit=events.append)
    r = reg.register(BASE)
    assert r.hash == canonical_hash(BASE) and r.id == "example.crm" and reg.ids() == ["example.crm"]
    reordered = json.loads(json.dumps(BASE, sort_keys=True))
    assert canonical_hash(reordered) == r.hash
    assert events == [{"event": "declaration_registered", "declaration_id": "example.crm", "declaration_version": "1.0.0", "declaration_hash": r.hash}]


def test_the_snapshot_cannot_be_changed_from_outside():
    src = copy.deepcopy(BASE)
    reg = DeclarationRegistry()
    r = reg.register(src)
    src["operations"][0]["approval"] = "always"           # the caller edits its own copy afterwards
    src["network"]["hosts"].append("evil.example.com")
    assert r.to_dict() == BASE
    with pytest.raises(TypeError):
        r.data["id"] = "x"
    with pytest.raises(TypeError):
        r.data["network"]["hosts"] = ()
    with pytest.raises(AttributeError):
        r.id = "x"
    copy_out = r.to_dict()
    copy_out["operations"].append({"name": "evil", "effect": "execute"})
    assert [o["name"] for o in r.to_dict()["operations"]] == ["update_note", "list_notes"]


def test_the_same_id_with_a_different_declaration_is_rejected_and_audited_the_same_declaration_is_a_no_op():
    events = []
    reg = DeclarationRegistry(audit=events.append)
    first = reg.register(BASE)
    assert reg.register(copy.deepcopy(BASE)) is first
    other = decl(version="1.0.1")
    with pytest.raises(RegistryError) as e:
        reg.register(other)
    assert e.value.code == "declaration_conflict"
    rejected = [x for x in events if x["event"] == "declaration_rejected"][0]
    assert rejected["reason"] == "declaration_conflict" and rejected["registered_hash"] == first.hash and rejected["declaration_hash"] == canonical_hash(other)
    assert reg.get("example.crm").hash == first.hash      # the registered one is untouched


def test_registration_is_refused_after_startup():
    events = []
    reg = DeclarationRegistry(audit=events.append)
    reg.register(BASE)
    reg.seal()
    with pytest.raises(RegistryError) as e:
        reg.register(decl(id="example.other"))
    assert e.value.code == "registry_sealed" and reg.ids() == ["example.crm"]
    assert events[-1]["event"] == "declaration_rejected" and events[-1]["reason"] == "registry_sealed"


def test_a_protected_secret_or_an_invalid_declaration_does_not_register():
    reg = DeclarationRegistry()
    for bad in (decl(secrets=["DATABASE_URL"]), {"schema": "ragleap.capability/1"}, decl(version="one")):
        with pytest.raises(RegistryError) as e:
            reg.register(bad)
        assert e.value.code == "invalid_declaration" and e.value.details
    assert reg.ids() == []


# ---------------------------------------------------------------- host-measured usage

def test_the_mismatch_rule_and_its_tolerance():
    m = Usage(calls=2, bytes_in=1000, bytes_out=100)
    assert mismatched_fields(m, Usage(calls=2, bytes_in=1050, bytes_out=140)) == []
    assert mismatched_fields(m, Usage(calls=0, bytes_in=0, bytes_out=0)) == ["calls", "bytes_in", "bytes_out"]
    assert mismatched_fields(m, Usage(calls=3, bytes_in=1000, bytes_out=100)) == ["calls"]


def test_a_misreport_makes_an_audit_event_with_both_numbers_and_the_declaration_hash():
    p = resolve_policy(BASE, "update_note", declaration_hash="h" * 64)
    ev = usage_mismatch_event(p, Usage(calls=2, bytes_in=500), Usage(calls=0))
    assert ev["event"] == "usage_mismatch" and ev["declaration_hash"] == "h" * 64 and "calls" in ev["fields"]
    assert ev["measured"]["calls"] == 2 and ev["reported"]["calls"] == 0
    assert usage_mismatch_event(p, Usage(calls=1), Usage(calls=1)) is None


def test_only_measured_usage_counts_toward_the_budget():
    p = resolve_policy(decl(budgets={"max_calls_per_run": 2, "max_bytes": 100}), "update_note")
    checker = BudgetChecker(MemoryCounterStore())
    assert checker.reserve(p, "run-1", "2026-11-20") is None and checker.reserve(p, "run-1", "2026-11-20") is None
    d = checker.reserve(p, "run-1", "2026-11-20")
    assert d is not None and d.error == denials.E_BUDGET_EXCEEDED and d.counter == "tool_calls_per_run"
    # bytes: the connector claims zero, the host measured 150: the budget is used up
    over = checker.record_bytes(p, "run-1", Usage(bytes_in=100, bytes_out=50))
    assert over is not None and over.counter == "tool_bytes_per_run" and over.value == 150 and over.limit == 100


def test_budgets_are_counted_per_run_and_per_day_separately():
    p = resolve_policy(decl(budgets={"max_calls_per_run": 1, "max_calls_per_day": 2}), "update_note")
    ch = BudgetChecker(MemoryCounterStore())
    assert ch.reserve(p, "run-a", "d1") is None
    assert ch.reserve(p, "run-b", "d1") is None            # another run, same day
    d = ch.reserve(p, "run-c", "d1")
    assert d.counter == "tool_calls_per_day"
    assert ch.reserve(p, "run-d", "d2") is None            # a new day


# ---------------------------------------------------------------- fail closed, with a clear message

def test_when_the_counter_store_is_down_the_action_is_denied_and_the_message_names_counter_and_store():
    p = resolve_policy(BASE, "update_note")
    store = MemoryCounterStore(unavailable=True)
    store.name = "database table tool_usage"
    d = BudgetChecker(store).reserve(p, "run-1", "2026-11-20")
    assert d is not None and d.error == denials.E_BUDGET_STORE_UNAVAILABLE
    assert (d.counter, d.store, d.connector, d.operation) == ("tool_calls_per_run", "database table tool_usage", "example.crm", "update_note")
    assert d.to_dict()["status"] == "denied"
    text = d.user_text()
    assert "tool_calls_per_run" in text and "database table tool_usage" in text and "Nothing was executed" in text
    ev = d.audit_event()
    assert ev["event"] == "budget_denied_store_unavailable" and ev["detail"] == "StoreUnavailable"


def test_an_unexpected_store_error_also_closes_the_door():
    class Broken(MemoryCounterStore):
        def incr_if_below(self, key, limit):
            raise RuntimeError("password=hunter2 in a message")
    p = resolve_policy(BASE, "update_note")
    d = BudgetChecker(Broken()).reserve(p, "r", "d")
    assert d.error == denials.E_BUDGET_STORE_UNAVAILABLE and d.detail == "RuntimeError"
    assert "hunter2" not in json.dumps(d.audit_event())


def test_a_byte_record_failure_is_also_a_denial_and_exceeded_differs_from_unavailable():
    p = resolve_policy(BASE, "update_note")
    assert BudgetChecker(MemoryCounterStore(unavailable=True)).record_bytes(p, "r", Usage(bytes_in=1)).error == denials.E_BUDGET_STORE_UNAVAILABLE
    full = BudgetChecker(MemoryCounterStore())
    p1 = resolve_policy(decl(budgets={"max_calls_per_run": 1}), "update_note")
    full.reserve(p1, "r", "d")
    exceeded = full.reserve(p1, "r", "d")
    assert exceeded.error == denials.E_BUDGET_EXCEEDED and "used up" in exceeded.user_text()
    assert exceeded.to_dict()["limit"] == 1 and exceeded.audit_event()["event"] == "budget_denied_exceeded"


def test_a_denied_policy_is_not_counted_and_not_denied_again():
    p = resolve_policy(BASE, "nonexistent")
    store = MemoryCounterStore(unavailable=True)
    assert BudgetChecker(store).reserve(p, "r", "d") is None       # nothing to budget: the policy already denies it


def test_token_budgets_keep_failing_open_even_when_their_store_is_down(monkeypatch):
    """The two behaviours are different on purpose: tokens control cost (fail open), actions control safety (fail closed)."""
    from core import budget
    monkeypatch.setenv("BUDGET_DAILY_TOKENS", "100")

    def down(role, period):
        raise RuntimeError("database down")
    monkeypatch.setattr(budget, "_used", down)
    assert budget.check_budget(None) is None                        # the LLM call is allowed
    p = resolve_policy(BASE, "update_note")
    assert BudgetChecker(MemoryCounterStore(unavailable=True)).reserve(p, "r", "d") is not None   # the action is not


# ---------------------------------------------------------------- the new package stays inside the rules

def test_the_policy_modules_use_no_network_environment_or_subprocess():
    import ragleap_boundary_scan as scan
    for p in sorted((ROOT / "core" / "connector_host").glob("*.py")):
        violations, _ = scan.scan_connector(p.read_text(encoding="utf-8"))
        assert violations == [], f"{p.name}: {violations}"
