"""ConnectorHost and the in-process implementation (Phase 0, draft).

The in-process host runs a connector object inside the RagLeap process. It enforces by convention only: a connector
that imports the network or reads the environment is not stopped here (the boundary scan reports it). Trust statement:
in-process connectors are trusted, reviewed code until process isolation exists.
"""
import copy
import json
import time
import urllib.parse
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from core.connector_host import contract as c
from core.connector_host.services import HostServices, HttpResult


class ConnectorHost(ABC):
    """The interface that every host implements (in-process now, isolated later)."""

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        """The connector's declaration (a copy)."""

    @abstractmethod
    def invoke(self, request: Any) -> c.Reply:
        """Run one operation. Never raises for a connector or contract problem: it returns a reply."""


Transport = Callable[[str, str, Dict[str, str], Optional[bytes], Optional[float]], HttpResult]


class MeteredServices(HostServices):
    """HostServices for one request. It counts what really crossed the boundary: these numbers, not the connector's,
    are what limits use."""

    def __init__(self, declaration: Dict[str, Any], transport: Optional[Transport], secrets: Optional[Callable[[str], Optional[str]]]):
        net = declaration.get("network") or {}
        self._hosts = {h.lower() for h in net.get("hosts", [])}
        self._secrets_declared = set(declaration.get("secrets", []))
        self._transport, self._secret_source = transport, secrets
        self.calls, self.bytes_in, self.bytes_out = 0, 0, 0
        self.egress: List[c.Egress] = []
        self.events: List[Dict[str, Any]] = []

    def http(self, method, url, *, headers=None, body=None, timeout_s=None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https":
            raise c.HostError(c.E_INSECURE_URL, "only https is allowed")
        host = (parts.hostname or "").lower()
        if host not in self._hosts:
            raise c.HostError(c.E_HOST_NOT_DECLARED, f"{host or '(none)'} is not a declared host")
        if self._transport is None:
            raise c.HostError(c.E_NO_TRANSPORT, "this host has no network transport")
        self.calls += 1
        self.bytes_out += len(body or b"")
        result = self._transport(method.upper(), url, dict(headers or {}), body, timeout_s)
        self.bytes_in += len(result.body or b"")
        self.egress.append(c.Egress(kind="http", host=host, method=method.upper(), status=int(result.status)))
        return result

    def tcp(self, host, port, purpose):
        raise c.HostError(c.E_UNSUPPORTED, "tcp is reserved in host services version 1")

    def secret(self, name):
        if name not in self._secrets_declared:
            raise c.HostError(c.E_SECRET_NOT_DECLARED, f"{name} is not declared")
        value = self._secret_source(name) if self._secret_source else None
        if value is None:
            raise c.HostError(c.E_SECRET_UNAVAILABLE, f"{name} has no value")
        return value

    def log(self, event):
        if len(self.events) < 100 and isinstance(event, dict):
            self.events.append(copy.deepcopy(event))


class InProcessHost(ConnectorHost):
    """Runs `connector` (an object with `invoke(request, services) -> dict`) for the given declaration.

    transport: a callable doing the actual https request. None means the host has no network (the default; the shared
    HTTP client of a later phase will provide a real one). secrets: a callable name -> value or None.
    """

    def __init__(self, declaration: Dict[str, Any], connector: Any, *, transport: Optional[Transport] = None,
                 secrets: Optional[Callable[[str], Optional[str]]] = None, clock: Callable[[], float] = time.monotonic):
        self._declaration = copy.deepcopy(declaration)
        self._operations = {o["name"] for o in self._declaration.get("operations", []) if isinstance(o, dict) and "name" in o}
        self._connector, self._transport, self._secrets, self._clock = connector, transport, secrets, clock

    def describe(self):
        return copy.deepcopy(self._declaration)

    def invoke(self, request):
        try:
            req = request if isinstance(request, c.Request) else c.Request.from_dict(request)
        except c.ContractError as e:
            return c.failed(str(request.get("id", "")) if isinstance(request, dict) else "", c.E_BAD_REQUEST, str(e))
        if req.v != c.HOST_VERSION:
            return c.denied(req.id, c.E_UNSUPPORTED_VERSION, f"this host speaks {c.HOST_VERSION}")
        if req.operation not in self._operations:
            return c.denied(req.id, c.E_UNDECLARED_OPERATION, f"{req.operation} is not declared")
        services = MeteredServices(self._declaration, self._transport, self._secrets)
        started = self._clock()
        try:
            out = self._connector.invoke(req, services)
        except c.HostError as e:
            return self._reply(req, services, started, c.STATUS_ERROR, None, e.code, e.message)
        except Exception as e:   # a connector bug must never escape; only the class name is kept (no text that could hold a secret)
            return self._reply(req, services, started, c.STATUS_ERROR, None, c.E_CONNECTOR_ERROR, type(e).__name__)
        if not isinstance(out, dict) or out.get("status") not in (c.STATUS_OK, c.STATUS_ERROR):
            return self._reply(req, services, started, c.STATUS_ERROR, None, c.E_CONNECTOR_ERROR, "invalid reply from the connector")
        return self._reply(req, services, started, out["status"], out.get("data"),
                           out.get("error_code", "") if out["status"] == c.STATUS_ERROR else "", str(out.get("error_message", ""))[:200],
                           reported=out.get("usage"))

    def _reply(self, req, services, started, status, data, code, message, reported=None):
        try:
            size = len(json.dumps(data)) if data is not None else 0
        except (TypeError, ValueError):
            return c.failed(req.id, c.E_CONNECTOR_ERROR, "reply data is not JSON-compatible")
        if size > c.MAX_REPLY_BYTES:
            status, data, code, message = c.STATUS_ERROR, None, c.E_REPLY_TOO_LARGE, f"reply is larger than {c.MAX_REPLY_BYTES} bytes"
        if code and code not in c.ERROR_CODES:
            code = c.E_CONNECTOR_ERROR
        return c.Reply(id=req.id, status=status, data=data, error_code=code, error_message=message,
                       usage=c.Usage(calls=services.calls, bytes_in=services.bytes_in, bytes_out=services.bytes_out,
                                     ms=int((self._clock() - started) * 1000)),
                       reported_usage=c.Usage.from_reported(reported), egress=list(services.egress))
