"""Host-measured usage and fail-closed action budgets (Phase 0, draft). Not wired into the gate yet.

Only what the HOST measured is enforced. What a connector reports is informational: a mismatch is audited, never trusted.
Counters live behind a CounterStore. If the store cannot be read or written, the action is DENIED (fail closed) with a
structured denial that names the counter and the store.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from core.connector_host import denials
from core.connector_host.contract import Usage
from core.connector_host.policy import EffectivePolicy

CALLS_PER_RUN, CALLS_PER_DAY, BYTES_PER_RUN = "tool_calls_per_run", "tool_calls_per_day", "tool_bytes_per_run"
BYTES_TOLERANCE_RATIO, BYTES_TOLERANCE_MIN = 0.10, 64


def mismatched_fields(measured: Usage, reported: Usage) -> List[str]:
    """Fields where the connector's report differs from the host's measurement beyond the tolerance."""
    out = []
    if reported.calls != measured.calls:
        out.append("calls")
    for f in ("bytes_in", "bytes_out"):
        m, r = getattr(measured, f), getattr(reported, f)
        if abs(r - m) > max(BYTES_TOLERANCE_MIN, int(BYTES_TOLERANCE_RATIO * m)):
            out.append(f)
    return out


def usage_mismatch_event(policy: EffectivePolicy, measured: Usage, reported: Usage) -> Optional[Dict[str, Any]]:
    fields = mismatched_fields(measured, reported)
    if not fields:
        return None
    return {"event": "usage_mismatch", "declaration_id": policy.declaration_id, "declaration_version": policy.declaration_version,
            "declaration_hash": policy.declaration_hash, "operation": policy.operation, "fields": fields,
            "measured": measured.to_dict(), "reported": reported.to_dict()}


class StoreUnavailable(Exception):
    """The counter store could not be read or written."""


class CounterStore(ABC):
    name = "unnamed store"      # shown to people in denials, for example 'database table tool_usage'

    @abstractmethod
    def incr_if_below(self, key: str, limit: int) -> Optional[int]:
        """Atomically add one if the value is below the limit. Returns the new value, or None when the limit is reached."""

    @abstractmethod
    def add(self, key: str, amount: int) -> int:
        """Add and return the new value."""

    @abstractmethod
    def read(self, key: str) -> int:
        """The current value (0 if unknown)."""


class MemoryCounterStore(CounterStore):
    """In-memory store for tests and tools. `unavailable=True` makes every call raise StoreUnavailable."""
    name = "memory counters"

    def __init__(self, unavailable: bool = False):
        self.values: Dict[str, int] = {}
        self.unavailable = unavailable

    def _check(self):
        if self.unavailable:
            raise StoreUnavailable("store is down")

    def incr_if_below(self, key, limit):
        self._check()
        v = self.values.get(key, 0)
        if v >= limit:
            return None
        self.values[key] = v + 1
        return v + 1

    def add(self, key, amount):
        self._check()
        self.values[key] = self.values.get(key, 0) + amount
        return self.values[key]

    def read(self, key):
        self._check()
        return self.values.get(key, 0)


class BudgetChecker:
    """Reserves calls before an action runs and records measured bytes after it. Fails closed."""

    def __init__(self, store: CounterStore):
        self.store = store

    def _key(self, scope: str, policy: EffectivePolicy, ident: str, what: str) -> str:
        return f"{scope}:{ident}:{policy.declaration_id}:{what}"

    def reserve(self, policy: EffectivePolicy, run_id: str, day: str) -> Optional[denials.Denial]:
        """Call BEFORE executing. Returns None when the call may go ahead, otherwise a Denial (nothing may run)."""
        if not policy.allowed:
            return None
        checks = [(CALLS_PER_RUN, self._key("run", policy, run_id, "calls"), policy.budgets.get("max_calls_per_run")),
                  (CALLS_PER_DAY, self._key("day", policy, day, "calls"), policy.budgets.get("max_calls_per_day"))]
        for counter, key, limit in checks:
            if not limit:
                continue
            try:
                if self.store.incr_if_below(key, limit) is None:
                    return denials.exceeded(counter, limit, limit, self.store.name, policy.declaration_id, policy.operation)
            except StoreUnavailable as e:
                return denials.store_unavailable(counter, self.store.name, policy.declaration_id, policy.operation, type(e).__name__)
            except Exception as e:      # any store failure is a closed door, never an open one
                return denials.store_unavailable(counter, self.store.name, policy.declaration_id, policy.operation, type(e).__name__)
        return None

    def record_bytes(self, policy: EffectivePolicy, run_id: str, measured: Usage) -> Optional[denials.Denial]:
        """Call AFTER executing, with the HOST's measurement. Returns a Denial when the byte budget is now used up or the store is down."""
        limit = policy.budgets.get("max_bytes")
        if not limit:
            return None
        key = self._key("run", policy, run_id, "bytes")
        try:
            total = self.store.add(key, measured.bytes_in + measured.bytes_out)
        except Exception as e:
            return denials.store_unavailable(BYTES_PER_RUN, self.store.name, policy.declaration_id, policy.operation, type(e).__name__)
        if total > limit:
            return denials.exceeded(BYTES_PER_RUN, total, limit, self.store.name, policy.declaration_id, policy.operation)
        return None
