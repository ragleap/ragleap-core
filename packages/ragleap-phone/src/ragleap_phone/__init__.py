"""ragleap-phone: planned phone calls for RagLeap. Skeleton only: no backends, no calls."""

from .adapter import Adapter, Call, Capabilities, Event, EVENT_KINDS, new_call_id
from .frame import Frame

__version__ = "0.0.0"
__all__ = ["Adapter", "Call", "Capabilities", "Event", "EVENT_KINDS", "Frame", "new_call_id", "__version__"]
