"""ragleap_agents - a small, provider-neutral agent loop over ragleap_tools Tool objects.

You supply the model as a plain callable llm(prompt: str) -> str and a list of
ragleap_tools.Tool objects; the library runs the act-observe loop, enforces
step limits and an approval gate, and lets a paused run be resumed later.
It has no database and no provider code. See agent.py for the safety model.
"""

from ragleap_agents.agent import (
    HARD_MAX_STEPS,
    TRUSTED,
    Agent,
    Policy,
    ResumeError,
    RunResult,
    ToolPolicy,
    parse_plan,
    validate_arguments,
)
from ragleap_agents.providers import ProviderError, openai_compatible
from ragleap_agents.delegate import MAX_DEPTH_CEILING, Budget, delegate_policy, make_delegate_tool
from ragleap_agents.state import AtomicStateStore, InMemoryStateStore, SQLiteStateStore, StateStore

__version__ = "0.5.1"

__all__ = [
    "Agent",
    "HARD_MAX_STEPS",
    "AtomicStateStore",
    "Budget",
    "MAX_DEPTH_CEILING",
    "delegate_policy",
    "make_delegate_tool",
    "InMemoryStateStore",
    "SQLiteStateStore",
    "Policy",
    "ProviderError",
    "ResumeError",
    "RunResult",
    "StateStore",
    "TRUSTED",
    "ToolPolicy",
    "openai_compatible",
    "parse_plan",
    "validate_arguments",
    "__version__",
]
