"""Error types. Every refusal is a typed error with a short machine-readable code."""
from __future__ import annotations


class PhysicalError(Exception):
    """Base class for all errors raised by ragleap-physical."""


class ValidationError(PhysicalError, ValueError):
    """Data from an adapter or caller does not match the schema."""


class ConfigError(PhysicalError, ValueError):
    """The envelope or bridge configuration is invalid."""


class EnvelopeViolation(PhysicalError):
    """A command is outside the safety envelope. ``code`` says which rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ApprovalError(PhysicalError):
    """Approval is missing, changed, expired or already used."""


class CapabilityError(PhysicalError):
    """The bridge's capability level does not allow this call."""


class SafetyStop(PhysicalError):
    """The bridge is stopped. Only an operator can clear it."""
