"""ragleap_agents.delegate - the building blocks of sub-agents (design: issue #827).

A child agent runs to completion inside one tool call of its parent, so its answer
reaches the parent as an ordinary, untrusted, fenced observation. This module holds
the pieces of sub-agents:

- delegate_policy(children): checks the children and returns the ToolPolicy to declare
  for the delegate tool in the parent's Policy. Everything fails closed: a child tool
  with no declared policy raises, an outbound child tool raises unless allow_outbound=True
  is passed explicitly, and the delegate tool is never outbound itself.
- Budget: how much delegation one run may use.
- check_max_depth: depth is 1 to MAX_DEPTH_CEILING.

A child cannot ask a human for approval (see the design issue), so a child tool that
would need approval is never run on a child's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ragleap_tools import Tool, ToolResult

from ragleap_agents.agent import _NAME_RE, Agent, ToolPolicy
from ragleap_agents.context import current_run

MAX_DEPTH_CEILING = 3


def _positive_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what} must be an integer of at least 1")
    return value


@dataclass(frozen=True)
class Budget:
    """What one run may spend on delegation: how many delegate calls, and how many
    steps all its children may take together."""

    max_delegations: int = 4
    max_child_steps: int = 16

    def __post_init__(self) -> None:
        _positive_int(self.max_delegations, "max_delegations")
        _positive_int(self.max_child_steps, "max_child_steps")


def check_max_depth(max_depth: Any) -> int:
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= MAX_DEPTH_CEILING:
        raise ValueError(f"max_depth must be an integer from 1 to {MAX_DEPTH_CEILING}")
    return max_depth


def delegate_policy(children: Dict[str, Agent], *, allow_outbound: bool = False) -> ToolPolicy:
    """Validate the children and return the ToolPolicy to declare for their delegate tool.

    taints is True if any child tool can taint (a nested delegate tool counts by the policy
    declared for it in the child's Policy). outbound is always False: delegating is not itself
    an action on the outside world. Raises ValueError for anything unsafe or unclear."""
    if not isinstance(allow_outbound, bool):
        raise ValueError("allow_outbound must be True or False")
    if not isinstance(children, dict) or not children:
        raise ValueError("children must be a non-empty dict of name -> Agent")
    taints = False
    for name, child in children.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"invalid child name {str(name)[:50]!r}: use 1-64 of A-Z a-z 0-9 _ . -")
        if not isinstance(child, Agent):
            raise ValueError(f"child {name!r} must be an Agent")
        for tool_name in child.tools:
            if tool_name not in child.policy.tools:
                raise ValueError(
                    f"child {name!r}: tool {tool_name!r} has no declared policy "
                    "(an undeclared tool is both tainting and outbound); declare it in the child's Policy")
            declared = child.policy.tools[tool_name]
            if declared.outbound and not allow_outbound:
                raise ValueError(
                    f"child {name!r}: tool {tool_name!r} is outbound; pass allow_outbound=True to allow it "
                    "(a child cannot ask for approval, so it stops instead of running it after a taint)")
            taints = taints or declared.taints
    return ToolPolicy(taints=taints, outbound=False, requires_approval=False)


ANSWER_CAP = 800  # characters of a child's answer returned to the parent


def _stop_unapproved(child: Agent, res: Any) -> Any:
    """A child paused for approval. Nobody can give it, so close its saved run as stopped, never to be resumed."""
    try:
        state = child.store.load(res.run_id)
    except ValueError:
        return res
    if state is None:
        return res
    return child._finish(state, "stopped", "approval_not_available", use_llm=False)


def make_delegate_tool(children: Dict[str, Agent], *, max_depth: int = 1, budget: Any = None,
                       allow_outbound: bool = False, name: str = "delegate") -> Tool:
    """An ordinary Tool that hands one task to a named child agent and returns its short answer.

    Declare delegate_policy(children, allow_outbound=...) for it in the parent's Policy. The child runs to
    completion inside this tool call; its answer comes back as an untrusted, fenced observation. A child that
    reaches an action needing approval is stopped with stop_reason "approval_not_available" and the action
    does not run. Depth, the number of delegations and the children's steps are limited per root run (the
    counters live in the root run's saved state; a child's own step cap bounds any overshoot)."""
    check_max_depth(max_depth)
    if budget is None:
        budget = Budget()
    elif not isinstance(budget, Budget):
        raise ValueError("budget must be a Budget")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError("invalid tool name: use 1-64 of A-Z a-z 0-9 _ . -")
    delegate_policy(children, allow_outbound=allow_outbound)
    kids = dict(children)
    names = sorted(kids)

    def handler(agent: Any, task: Any) -> ToolResult:
        ctx = current_run()
        if ctx is None:
            return ToolResult(False, error="delegation is only available inside an agent run")
        child = kids.get(agent) if isinstance(agent, str) else None
        if child is None:
            return ToolResult(False, error="unknown agent")
        if not isinstance(task, str):
            return ToolResult(False, error="task must be a string")
        if ctx.depth + 1 > max_depth:
            return ToolResult(False, error="delegation depth limit reached")
        counters = ctx.delegation
        if counters["delegations"] >= budget.max_delegations:
            return ToolResult(False, error="delegation budget used up")
        if counters["child_steps"] >= budget.max_child_steps:
            return ToolResult(False, error="delegation step budget used up")
        counters["delegations"] += 1
        res = child.run(task, tainted=ctx.tainted, scope=ctx.scope)
        counters["child_steps"] += len(res.steps)
        if res.status == "awaiting_approval":
            res = _stop_unapproved(child, res)
        if res.status == "done":
            return ToolResult(True, result={"child_run": res.run_id, "answer": (res.answer or "")[:ANSWER_CAP]})
        reason = "approval_not_available" if res.status == "awaiting_approval" else res.stop_reason
        return ToolResult(False, error=f"agent {agent} {res.status} ({reason}); run {res.run_id}")

    schema = {
        "type": "object",
        "properties": {"agent": {"type": "string", "enum": names}, "task": {"type": "string"}},
        "required": ["agent", "task"],
    }
    return Tool(name, "Hand one task to a sub-agent and get its short answer. Agents: " + ", ".join(names) + ".", schema, handler)
