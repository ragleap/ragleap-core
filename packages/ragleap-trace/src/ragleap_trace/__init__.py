"""ragleap-trace: standard-library-only tracing with hard limits and privacy-safe defaults."""

from ._span import (
    DENIED_KEYS,
    MAX_ATTRIBUTES,
    MAX_KEY_LEN,
    MAX_NAME_LEN,
    MAX_STR_LEN,
    Span,
)
from ._tracer import Tracer, traced
from .exporters import InMemoryExporter, JsonlExporter
from .rag import RagTraceAdapter

__version__ = "0.1.0"

__all__ = [
    "DENIED_KEYS",
    "MAX_ATTRIBUTES",
    "MAX_KEY_LEN",
    "MAX_NAME_LEN",
    "MAX_STR_LEN",
    "InMemoryExporter",
    "JsonlExporter",
    "RagTraceAdapter",
    "Span",
    "Tracer",
    "traced",
    "__version__",
]
