"""Proposal format v0. The model can only ask for a proposal; it never sends a device command."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError
from .observation import actor, finite, ident, unit

SCHEMA = "ragleap.physical.proposal/0"
MAX_REASON = 500
QUANTITIES = ("pos", "vel")
_ALLOWED_FORMAT = {"\u200c", "\u200d"}  # joiners that Indic scripts need


def _printable(text: str) -> bool:
    """Letters, marks, numbers, punctuation, symbols and the plain space only. No controls, no bidi or invisible
    characters, no line breaks: the text is shown to the person who approves."""
    return all(ch == " " or ch in _ALLOWED_FORMAT or unicodedata.category(ch)[0] in "LMNPS" for ch in text)


@dataclass(frozen=True)
class ExpectedOutcome:
    """A machine-checkable test run after apply. Not a model's word."""

    entity: str
    quantity: str
    lo: float
    hi: float
    unit: str
    by_sim_time_s: float

    def __post_init__(self) -> None:
        ident(self.entity, "outcome.entity")
        if self.quantity not in QUANTITIES:
            raise ValidationError("outcome.quantity must be pos or vel")
        lo, hi = finite(self.lo, "outcome.lo"), finite(self.hi, "outcome.hi")
        if lo > hi:
            raise ValidationError("outcome.lo must not exceed outcome.hi")
        unit(self.unit)
        if finite(self.by_sim_time_s, "outcome.by_sim_time_s") <= 0:
            raise ValidationError("outcome.by_sim_time_s must be positive")


@dataclass(frozen=True)
class Proposal:
    """A described action. ``reason`` is untrusted display text and is never interpreted."""

    proposal_id: str
    org_id: str
    requested_by: str
    target: str
    command: str
    value: float
    value_unit: str
    expected_outcome: ExpectedOutcome
    state_ref: str
    reason: str
    created_at: float
    expires_at: float
    capability: str = "control"
    dry_run: bool = True

    def __post_init__(self) -> None:
        for name in ("proposal_id", "org_id", "target", "command", "state_ref"):
            ident(getattr(self, name), name)
        actor(self.requested_by, "requested_by")
        finite(self.value, "value")
        unit(self.value_unit)
        if not isinstance(self.reason, str) or len(self.reason) > MAX_REASON or not _printable(self.reason):
            raise ValidationError("reason must be printable single-line text of at most 500 characters")
        if finite(self.expires_at, "expires_at") <= finite(self.created_at, "created_at"):
            raise ValidationError("expires_at must be after created_at")
        if self.capability != "control":
            raise ValidationError("a proposal always has capability 'control'")

    def to_dict(self) -> dict:
        """JSON-ready dict following the v0 schema."""
        o = self.expected_outcome
        return {
            "schema": SCHEMA, "proposal_id": self.proposal_id, "org_id": self.org_id,
            "requested_by": self.requested_by, "capability": self.capability, "target": self.target,
            "command": self.command, "params": {"value": self.value, "unit": self.value_unit},
            "expected_outcome": {"entity": o.entity, "quantity": o.quantity, "within": [o.lo, o.hi],
                                 "unit": o.unit, "by_sim_time_s": o.by_sim_time_s},
            "state_ref": self.state_ref, "reason": self.reason, "created_at": self.created_at,
            "expires_at": self.expires_at, "dry_run": self.dry_run,
        }

    def canonical(self) -> bytes:
        """Stable bytes of the whole proposal (sorted keys, no spaces)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("ascii")

    def digest(self) -> str:
        """SHA-256 of the canonical proposal. An approval binds to this value."""
        return hashlib.sha256(self.canonical()).hexdigest()
