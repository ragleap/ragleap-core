"""ragleap_flows - multi-tenant flows as plain JSON data: tool steps, approval checkpoints and branches.

Every run belongs to a ragleap_security Scope. See runner.py for the isolation rules and flow.py for the file
format and the hard limits. Not independently audited.
"""

from ragleap_agents import ResumeError

from ragleap_flows.flow import (DEADLINE_SECONDS, MAX_FLOW_BYTES, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, MAX_STATE_BYTES,
                                MAX_STEPS, Flow, FlowError, from_dict, from_json)
from ragleap_flows.runner import FlowResult, FlowRunner

__version__ = "0.1.0"

__all__ = [
    "DEADLINE_SECONDS", "Flow", "FlowError", "FlowResult", "FlowRunner", "MAX_FLOW_BYTES", "MAX_INPUT_BYTES",
    "MAX_OUTPUT_BYTES", "MAX_STATE_BYTES", "MAX_STEPS", "ResumeError", "__version__", "from_dict", "from_json",
]
