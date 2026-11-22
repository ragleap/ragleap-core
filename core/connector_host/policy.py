"""Effective policy resolution (Phase 0, draft). A pure function: nothing in the gate calls it yet.

effective policy = the core's floor, the owner's configuration and the declaration, combined. A declaration can only
NARROW what is allowed; it can never widen it, and it can never remove a rule the core applies. The result is a frozen
record that can be written into an audit event (compact form, with a stable hash), so the log shows what was actually
applied and not only what was declared.
"""
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

# The core's own tables for its 17 built-in tools. They duplicate facts that live in core/agent_loop.py and
# core/autonomy.py on purpose (this package must not import the gate); tests/test_connector_policy.py compares them
# with the real sets, so they cannot drift apart silently.
BUILTIN_EFFECT = {
    "send_webhook": "external_send", "send_slack": "external_send", "send_email": "external_send",
    "update_crm_record": "external_send", "schedule_event": "external_send", "notify_owner": "external_send",
    "mcp_call": "external_send", "create_task": "write", "run_code": "execute", "run_shell": "execute",
    "fetch_page": "read", "list_files": "read", "read_file": "read", "preview_change": "read",
    "write_file": "write", "delete_file": "write", "request_access": "write", "delegate": "execute",
}
OUTBOUND_BUILTINS = frozenset({"send_webhook", "send_slack", "send_email", "update_crm_record", "schedule_event",
                               "mcp_call", "fetch_page", "write_file", "delete_file"})
TAINTING_BUILTINS = frozenset({"fetch_page", "mcp_call", "list_files", "read_file", "preview_change"})
ALWAYS_APPROVAL_OPERATIONS = frozenset({"write_file", "delete_file", "request_access"})
# Operations denied unless the owner grants them by name (a declaration cannot remove this). `delegate` starts sub-agents.
GRANT_REQUIRED_OPERATIONS = frozenset({"delegate"})
# How far an operation reaches: none < fixed_hosts < owner_configured < runtime_chosen. A built-in tool is never declared lower.
EGRESS_RANK = {"none": 0, "fixed_hosts": 1, "owner_configured": 2, "runtime_chosen": 3}
BUILTIN_EGRESS = {"send_webhook": "owner_configured", "send_slack": "owner_configured", "send_email": "owner_configured",
                  "update_crm_record": "owner_configured", "schedule_event": "owner_configured",
                  "notify_owner": "owner_configured", "mcp_call": "owner_configured", "fetch_page": "runtime_chosen"}
_EGRESS_BASE = ("deny_when_egress_disabled", "block_metadata_and_link_local")
# What the core must apply per class once it enforces declarations (Phase 1); today they are only recorded.
EGRESS_OBLIGATIONS = {
    "none": (), "fixed_hosts": _EGRESS_BASE, "owner_configured": _EGRESS_BASE,
    "runtime_chosen": _EGRESS_BASE + ("block_non_public_before_and_after_dns", "revalidate_each_redirect_hop",
                                      "audit_resolved_url_and_status"),
}
EFFECT_RANK = {"read": 0, "write": 1, "execute": 2, "external_send": 3}
# Rules the gate applies at run time whatever any declaration says.
RUNTIME_FORCING = ("tainted_run", "sensitive_role")
CORE_CEILINGS = {"max_calls_per_run": 50, "max_calls_per_day": 1000, "max_bytes": 10 * 1024 * 1024, "timeout_s": 60}

D_UNDECLARED = "undeclared_operation"
D_AUTONOMY_OFF = "autonomy_off"
D_NOT_ALLOWED_BY_OWNER = "operation_not_allowed_by_owner"
D_NOT_GRANTED = "operation_not_granted"
D_EGRESS_DISABLED = "egress_disabled"


@dataclass(frozen=True)
class OwnerPolicy:
    """The owner's configuration. None means 'no owner restriction'; an empty set means 'nothing allowed'."""
    mode: str = "semi"                                  # off | semi | full
    allowed_operations: Optional[frozenset] = None
    allowed_hosts: Optional[frozenset] = None
    granted_operations: frozenset = frozenset()          # operations the owner granted by name; nothing is granted by default
    egress_enabled: bool = True                          # False: every operation that reaches outside is denied


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EffectivePolicy:
    allowed: bool
    declaration_id: str
    declaration_version: str
    declaration_hash: str
    operation: str
    denial_code: str = ""
    effect: str = ""
    approval: str = "per_mode"                          # always | per_mode (the gate then decides at run time)
    approval_reasons: Tuple[str, ...] = ()
    taint: str = "none"                                 # none | tainting | outbound | both
    runtime_forcing: Tuple[str, ...] = RUNTIME_FORCING
    hosts: Tuple[str, ...] = ()
    egress: str = "none"                                # none | fixed_hosts | owner_configured | runtime_chosen
    egress_obligations: Tuple[str, ...] = ()
    inherits: Mapping[str, Any] = field(default_factory=dict)
    scope_level: str = "organization"
    user_filter_field: str = ""
    budgets: Mapping[str, int] = field(default_factory=dict)
    narrowed: Tuple[str, ...] = ()                      # things the declaration asked for that were cut back (hosts)
    clamped: Tuple[str, ...] = ()                       # budgets that were cut to the core's ceilings
    notes: Tuple[str, ...] = ()

    def to_audit(self) -> Dict[str, Any]:
        """Compact, argument-free form for audit events: never arguments, never secret values, never secret names."""
        d = {"declaration": {"id": self.declaration_id, "version": self.declaration_version, "hash": self.declaration_hash},
             "operation": self.operation, "allowed": self.allowed, "denial_code": self.denial_code, "effect": self.effect,
             "approval": self.approval, "approval_reasons": list(self.approval_reasons), "taint": self.taint,
             "runtime_forcing": list(self.runtime_forcing), "hosts": list(self.hosts),
             "egress": self.egress, "egress_obligations": list(self.egress_obligations),
             "inherits": dict(sorted(self.inherits.items())),
             "scope": {"level": self.scope_level, "user_filter_field": self.user_filter_field},
             "budgets": dict(sorted(self.budgets.items())), "narrowed": list(self.narrowed), "clamped": list(self.clamped),
             "notes": list(self.notes)}
        d["policy_hash"] = canonical_hash(d)
        return d


def declared_egress(declaration: Any) -> str:
    """The egress class a declaration states or implies: its network.egress, else fixed_hosts when hosts or tcp are listed."""
    net = declaration.get("network") if isinstance(declaration, Mapping) else None
    net = net if isinstance(net, Mapping) else {}
    e = net.get("egress")
    if isinstance(e, str) and e in EGRESS_RANK:
        return e
    return "fixed_hosts" if (net.get("hosts") or net.get("tcp")) else "none"


def _taint_set(taint: str):
    return {"none": set(), "tainting": {"tainting"}, "outbound": {"outbound"}, "both": {"tainting", "outbound"}}.get(taint, set())


def _taint_name(s) -> str:
    return "both" if s >= {"tainting", "outbound"} else ("outbound" if "outbound" in s else ("tainting" if "tainting" in s else "none"))


def resolve_policy(declaration: Mapping[str, Any], operation: str, owner: OwnerPolicy = OwnerPolicy(),
                   ceilings: Optional[Mapping[str, int]] = None, declaration_hash: str = "") -> EffectivePolicy:
    """Combine the core floor, the owner's configuration and the declaration for one operation."""
    ceilings = dict(CORE_CEILINGS if ceilings is None else ceilings)
    decl = declaration if isinstance(declaration, dict) else json.loads(json.dumps(declaration, default=dict))
    dhash = declaration_hash or canonical_hash(decl)
    base = dict(declaration_id=str(decl.get("id", "")), declaration_version=str(decl.get("version", "")),
                declaration_hash=dhash, operation=operation)
    op = next((o for o in decl.get("operations", []) if isinstance(o, dict) and o.get("name") == operation), None)
    if op is None:
        return EffectivePolicy(allowed=False, denial_code=D_UNDECLARED, **base)
    if owner.mode == "off":
        return EffectivePolicy(allowed=False, denial_code=D_AUTONOMY_OFF, **base)
    if owner.allowed_operations is not None and operation not in owner.allowed_operations:
        return EffectivePolicy(allowed=False, denial_code=D_NOT_ALLOWED_BY_OWNER, **base)
    if (op.get("grant") == "explicit" or operation in GRANT_REQUIRED_OPERATIONS) and operation not in owner.granted_operations:
        return EffectivePolicy(allowed=False, denial_code=D_NOT_GRANTED, **base)
    # egress: never lower than the core's own class for a built-in tool; every class is denied when the owner turns egress off
    egress = declared_egress(decl)
    core_egress = BUILTIN_EGRESS.get(operation, "none")
    egress_raised = EGRESS_RANK[core_egress] > EGRESS_RANK[egress]
    if egress_raised:
        egress = core_egress
    if egress != "none" and not owner.egress_enabled:
        return EffectivePolicy(allowed=False, denial_code=D_EGRESS_DISABLED, egress=egress, **base)
    notes = []
    if egress_raised:
        notes.append("egress_raised_to_core")
    # effect: never weaker than the core's own classification of a built-in tool
    declared_effect = op.get("effect", "read")
    core_effect = BUILTIN_EFFECT.get(operation)
    effect = declared_effect
    if core_effect and EFFECT_RANK.get(core_effect, 0) > EFFECT_RANK.get(declared_effect, 0):
        effect = core_effect
        notes.append("effect_raised_to_core")
    # approval: a declaration can only ask for MORE approval
    reasons = []
    declared_approval = op.get("approval", "per_mode")
    if declared_approval == "always":
        reasons.append("declared")
    elif declared_approval not in ("per_mode", None):
        notes.append("invalid_approval_ignored")      # for example 'never': it does not exist and is never honoured
    if operation in ALWAYS_APPROVAL_OPERATIONS:
        reasons.append("core_floor:always_approval_operation")
    # taint: the union of the core's rule and the declaration; for operations the core does not know, the effect decides
    taint = set()
    if operation in BUILTIN_EFFECT:
        if operation in OUTBOUND_BUILTINS:
            taint.add("outbound")
        if operation in TAINTING_BUILTINS:
            taint.add("tainting")
    elif effect != "read":
        taint.add("outbound")
    taint |= _taint_set(op.get("taint", "none"))
    # hosts: intersection with the owner's list, never wider
    net = decl.get("network") or {}
    declared_hosts = [h for h in net.get("hosts", []) if isinstance(h, str)]
    narrowed = []
    if owner.allowed_hosts is not None:
        narrowed = [h for h in declared_hosts if h not in owner.allowed_hosts]
        declared_hosts = [h for h in declared_hosts if h in owner.allowed_hosts]
    # budgets: clamped to the core's ceilings; a missing budget means the ceiling
    declared_budgets = decl.get("budgets") or {}
    budgets, clamped = {}, []
    for k, ceiling in sorted(ceilings.items()):
        v = declared_budgets.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            budgets[k] = min(v, ceiling)
            if v > ceiling:
                clamped.append(k)
        else:
            budgets[k] = ceiling
    scope = decl.get("scope") or {}
    return EffectivePolicy(allowed=True, effect=effect, approval="always" if reasons else "per_mode",
                           approval_reasons=tuple(reasons), taint=_taint_name(taint), hosts=tuple(sorted(declared_hosts)), egress=egress, egress_obligations=EGRESS_OBLIGATIONS[egress],
                           inherits=dict(op["inherits"]) if isinstance(op.get("inherits"), dict) else {},
                           scope_level=scope.get("level", "organization"), user_filter_field=scope.get("user_filter_field", "") or "",
                           budgets=budgets, narrowed=tuple(sorted(narrowed)), clamped=tuple(clamped), notes=tuple(notes), **base)
