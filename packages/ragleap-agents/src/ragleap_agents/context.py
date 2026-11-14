"""ragleap_agents.context - which run a tool call belongs to (design: issue #827).

While the loop runs a tool it sets a RunContext in a contextvar, so a tool that starts another run
(the delegate tool) knows the depth, the taint of the calling run and the shared delegation counters.
Threads and async tasks do not share it, and it is reset when the tool returns.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RunContext:
    run_id: str
    depth: int  # 0 for a root run, parent depth + 1 for a run started inside a tool call
    tainted: bool  # the calling run's taint when the tool call started
    delegation: Dict[str, int]  # the root run's counters: {"delegations": n, "child_steps": m}
    scope: Optional[Any] = None  # the calling run's Scope (None for a run without one); a child run inherits it


_CURRENT: "contextvars.ContextVar[Optional[RunContext]]" = contextvars.ContextVar("ragleap_agents_run", default=None)


def current_run() -> Optional[RunContext]:
    return _CURRENT.get()


def enter_run(ctx: RunContext) -> contextvars.Token:
    return _CURRENT.set(ctx)


def leave_run(token: contextvars.Token) -> None:
    _CURRENT.reset(token)
