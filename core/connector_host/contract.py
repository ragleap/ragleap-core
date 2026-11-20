"""The host-connector contract, version ragleap.host/1 (Phase 0, draft).

A connector is anything that declares operations (see docs/capability-schema.md) and answers requests. The core is the
host. Request and reply are plain data (JSON-compatible) so the same messages can later cross a process boundary.
Nothing in the RagLeap core uses this module yet: it defines the contract that an in-process host (Phase 2) and an
isolated host (Phase 3) will both implement.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

HOST_VERSION = "ragleap.host/1"
MAX_REPLY_BYTES = 1024 * 1024        # a reply larger than this is refused by the host
MAX_ARGS_BYTES = 256 * 1024

STATUS_OK, STATUS_ERROR, STATUS_DENIED = "ok", "error", "denied"
STATUSES = (STATUS_OK, STATUS_ERROR, STATUS_DENIED)

# Error codes a host may return. A caller must treat unknown codes as a plain error.
E_BAD_REQUEST = "bad_request"
E_UNSUPPORTED_VERSION = "unsupported_version"
E_UNDECLARED_OPERATION = "undeclared_operation"
E_UNSUPPORTED = "unsupported"                 # a reserved service (tcp) that this host does not provide
E_NO_TRANSPORT = "no_transport"               # http was asked but this host has no transport
E_HOST_NOT_DECLARED = "host_not_declared"     # http to a host that the declaration does not list
E_INSECURE_URL = "insecure_url"               # http to a URL that is not https
E_SECRET_NOT_DECLARED = "secret_not_declared"
E_SECRET_UNAVAILABLE = "secret_unavailable"
E_CONNECTOR_ERROR = "connector_error"
E_REPLY_TOO_LARGE = "reply_too_large"
ERROR_CODES = frozenset({
    E_BAD_REQUEST, E_UNSUPPORTED_VERSION, E_UNDECLARED_OPERATION, E_UNSUPPORTED, E_NO_TRANSPORT, E_HOST_NOT_DECLARED,
    E_INSECURE_URL, E_SECRET_NOT_DECLARED, E_SECRET_UNAVAILABLE, E_CONNECTOR_ERROR, E_REPLY_TOO_LARGE,
})


class ContractError(ValueError):
    """A request or reply does not follow the contract."""


class HostError(Exception):
    """Raised inside a connector by a host service that refused the call; carries a contract error code."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code if code in ERROR_CODES else E_CONNECTOR_ERROR
        self.message = message


@dataclass(frozen=True)
class Context:
    run_id: str
    actor: str = ""                       # who or what asked (a role name or a user id); descriptive
    scope_level: str = "organization"     # organization | user
    user_id: Optional[str] = None
    deadline_ms: int = 20000
    budget_calls_left: Optional[int] = None
    budget_bytes_left: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "actor": self.actor, "scope_level": self.scope_level, "user_id": self.user_id,
                "deadline_ms": self.deadline_ms, "budget_calls_left": self.budget_calls_left,
                "budget_bytes_left": self.budget_bytes_left}

    @staticmethod
    def from_dict(d: Any) -> "Context":
        if not isinstance(d, dict):
            raise ContractError("context must be an object")
        unknown = set(d) - {"run_id", "actor", "scope_level", "user_id", "deadline_ms", "budget_calls_left", "budget_bytes_left"}
        if unknown:
            raise ContractError(f"context has unknown fields: {sorted(unknown)}")
        if not isinstance(d.get("run_id"), str) or not d["run_id"]:
            raise ContractError("context.run_id must be a non-empty string")
        if d.get("scope_level", "organization") not in ("organization", "user"):
            raise ContractError("context.scope_level must be organization or user")
        for k in ("deadline_ms", "budget_calls_left", "budget_bytes_left"):
            v = d.get(k)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                raise ContractError(f"context.{k} must be a non-negative integer")
        return Context(run_id=d["run_id"], actor=str(d.get("actor", "")), scope_level=d.get("scope_level", "organization"),
                       user_id=d.get("user_id"), deadline_ms=d.get("deadline_ms", 20000),
                       budget_calls_left=d.get("budget_calls_left"), budget_bytes_left=d.get("budget_bytes_left"))


@dataclass(frozen=True)
class Request:
    id: str
    operation: str
    args: Dict[str, Any]
    context: Context
    v: str = HOST_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {"v": self.v, "id": self.id, "operation": self.operation, "args": self.args, "context": self.context.to_dict()}

    @staticmethod
    def from_dict(d: Any) -> "Request":
        import json
        if not isinstance(d, dict):
            raise ContractError("request must be an object")
        unknown = set(d) - {"v", "id", "operation", "args", "context"}
        if unknown:
            raise ContractError(f"request has unknown fields: {sorted(unknown)}")
        for k in ("id", "operation"):
            if not isinstance(d.get(k), str) or not d[k]:
                raise ContractError(f"request.{k} must be a non-empty string")
        args = d.get("args", {})
        if not isinstance(args, dict):
            raise ContractError("request.args must be an object")
        try:
            if len(json.dumps(args)) > MAX_ARGS_BYTES:
                raise ContractError("request.args is too large")
        except (TypeError, ValueError):
            raise ContractError("request.args must be JSON-compatible")
        return Request(id=d["id"], operation=d["operation"], args=args, context=Context.from_dict(d.get("context")), v=d.get("v", ""))


@dataclass(frozen=True)
class Usage:
    calls: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    ms: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {"calls": self.calls, "bytes_in": self.bytes_in, "bytes_out": self.bytes_out, "ms": self.ms}

    @staticmethod
    def from_reported(d: Any) -> "Usage":
        """Usage as a connector reports it: only non-negative integers are kept, everything else is ignored."""
        if not isinstance(d, dict):
            return Usage()
        vals = {k: d.get(k, 0) for k in ("calls", "bytes_in", "bytes_out", "ms")}
        return Usage(**{k: (v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0) for k, v in vals.items()})


@dataclass(frozen=True)
class Egress:
    kind: str                       # http | tcp
    host: str
    method: str = ""
    status: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "host": self.host, "method": self.method, "status": self.status}


@dataclass
class Reply:
    id: str
    status: str
    data: Any = None
    error_code: str = ""
    error_message: str = ""
    usage: Usage = field(default_factory=Usage)            # measured by the host: this is what limits use
    reported_usage: Usage = field(default_factory=Usage)   # what the connector said: informational only
    egress: List[Egress] = field(default_factory=list)
    v: str = HOST_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {"v": self.v, "id": self.id, "status": self.status, "data": self.data,
                "error": {"code": self.error_code, "message": self.error_message} if self.error_code else None,
                "usage": self.usage.to_dict(), "reported_usage": self.reported_usage.to_dict(),
                "egress": [e.to_dict() for e in self.egress]}

    @staticmethod
    def from_dict(d: Any) -> "Reply":
        if not isinstance(d, dict):
            raise ContractError("reply must be an object")
        if d.get("status") not in STATUSES:
            raise ContractError("reply.status must be ok, error or denied")
        err = d.get("error") or {}
        return Reply(id=str(d.get("id", "")), status=d["status"], data=d.get("data"), error_code=err.get("code", ""),
                     error_message=err.get("message", ""), usage=Usage.from_reported(d.get("usage")),
                     reported_usage=Usage.from_reported(d.get("reported_usage")),
                     egress=[Egress(**{k: e.get(k, "" if k != "status" else 0) for k in ("kind", "host", "method", "status")}) for e in d.get("egress", [])],
                     v=d.get("v", HOST_VERSION))


def denied(request_id: str, code: str, message: str = "") -> Reply:
    return Reply(id=request_id, status=STATUS_DENIED, error_code=code, error_message=message)


def failed(request_id: str, code: str, message: str = "") -> Reply:
    return Reply(id=request_id, status=STATUS_ERROR, error_code=code, error_message=message)
