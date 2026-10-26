"""The four capability levels. A bridge declares the highest level it offers."""
from __future__ import annotations

from enum import IntEnum


class Capability(IntEnum):
    """observe < analyse < recommend < control."""

    OBSERVE = 1
    ANALYSE = 2
    RECOMMEND = 3
    CONTROL = 4
