"""Structured denials for budget and policy decisions (Phase 0, draft).

A denial always says which counter and which backing store were involved, so a person can tell a full budget from an
unreachable database. Action budgets fail CLOSED (nothing runs when the counter cannot be read). Token budgets
(core/budget.py) keep failing OPEN by design: they control cost, these control safety.
"""
from dataclasses import dataclass
from typing import Any, Dict

E_BUDGET_STORE_UNAVAILABLE = "budget_store_unavailable"
E_BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True)
class Denial:
    error: str
    counter: str
    store: str
    connector: str
    operation: str
    detail: str = ""
    value: int = -1
    limit: int = -1
    status: str = "denied"

    def to_dict(self) -> Dict[str, Any]:
        d = {"status": self.status, "error": self.error, "counter": self.counter, "store": self.store,
             "connector": self.connector, "operation": self.operation, "detail": self.detail}
        if self.error == E_BUDGET_EXCEEDED:
            d["value"], d["limit"] = self.value, self.limit
        return d

    def audit_event(self) -> Dict[str, Any]:
        """The audit event for this denial; `detail` is the exception class only, never a message that could hold a secret."""
        return dict(self.to_dict(), event="budget_denied_" + self.error.replace("budget_", ""))

    def user_text(self) -> str:
        if self.error == E_BUDGET_STORE_UNAVAILABLE:
            return (f"Action not run: the budget counter '{self.counter}' could not be read from its store ({self.store}). "
                    "Nothing was executed. Check the database and try again.")
        return (f"Action not run: the budget counter '{self.counter}' is used up ({self.value} of {self.limit}, store: {self.store}). "
                "Nothing was executed.")


def store_unavailable(counter: str, store: str, connector: str, operation: str, detail: str = "") -> Denial:
    return Denial(error=E_BUDGET_STORE_UNAVAILABLE, counter=counter, store=store, connector=connector, operation=operation, detail=detail)


def exceeded(counter: str, value: int, limit: int, store: str, connector: str, operation: str) -> Denial:
    return Denial(error=E_BUDGET_EXCEEDED, counter=counter, store=store, connector=connector, operation=operation, value=value, limit=limit)
