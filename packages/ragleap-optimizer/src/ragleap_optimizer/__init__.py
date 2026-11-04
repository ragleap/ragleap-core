"""ragleap-optimizer: a measure-first layer that reduces the token use of LLM requests.

Every step reports what it changed and whether it can lose information. Not independently audited.
See docs/threat-model.md."""
from ragleap_optimizer import steps
from ragleap_optimizer.cache import ExactCache
from ragleap_optimizer.evaluate import EvalTable, evaluate
from ragleap_optimizer.model import (InvalidRequestError, Optimized, OptimizerError, Report, StepReport,
                                     count_request, estimate_tokens)
from ragleap_optimizer.optimizer import Optimizer
from ragleap_optimizer.resilience import AllFailedError, retry, with_fallback

__version__ = "0.1.0"

__all__ = [
    "AllFailedError", "EvalTable", "ExactCache", "InvalidRequestError", "Optimized", "Optimizer", "OptimizerError",
    "Report", "StepReport", "__version__", "count_request", "estimate_tokens", "evaluate", "retry", "steps",
    "with_fallback",
]
