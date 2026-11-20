"""HostServices, version 1: the only things a connector may ask of its host (Phase 0, draft).

A connector never opens a socket, reads the environment or writes a log line to a secret itself. In the in-process host
this is a convention (the connector class can still bypass it; the boundary scan and the conformance checker report
that). In the isolated host (a later phase) the same four calls cross a process boundary and the connector has no
other way out.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""


class HostServices(ABC):
    @abstractmethod
    def http(self, method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
             body: Optional[bytes] = None, timeout_s: Optional[float] = None) -> HttpResult:
        """An https request to a host that the declaration lists. Raises HostError when the host refuses."""

    @abstractmethod
    def tcp(self, host: str, port: int, purpose: str) -> Any:
        """RESERVED in version 1: a connection factory for a declared network.tcp endpoint (for example a SQL
        database). Hosts that do not implement it raise HostError('unsupported')."""

    @abstractmethod
    def secret(self, name: str) -> str:
        """The value of a secret that the declaration lists. Raises HostError when it is not declared or unavailable."""

    @abstractmethod
    def log(self, event: Dict[str, Any]) -> None:
        """Record a small JSON-compatible event for the host's audit trail. Never pass secrets."""
