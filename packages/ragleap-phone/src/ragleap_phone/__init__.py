"""ragleap-phone: planned phone calls for RagLeap. Skeleton only: no backends, no calls."""

from .adapter import Adapter, Call, Capabilities, Event, EVENT_KINDS, new_call_id
from .audiopath import InboundAudio, LAWS, OutboundAudio
from .frame import Frame
from .resample import Resampler, SUPPORTED_RATE_PAIRS

__version__ = "0.0.0"
__all__ = ["Adapter", "Call", "Capabilities", "Event", "EVENT_KINDS", "Frame", "InboundAudio", "LAWS", "OutboundAudio",
           "Resampler", "SUPPORTED_RATE_PAIRS", "new_call_id", "__version__"]
