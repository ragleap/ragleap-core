"""The id of the request being handled, so log lines can say which request they belong to.

core/api.py makes (or accepts, when it is short and safe) one id per request and returns it as X-Request-ID. This
module keeps it in a context variable for the length of that request. It reaches code that runs inside the request,
including the worker thread of a normal route; it does not reach a background thread started later, such as the
one that continues an agent run after an approval.

AUDIT log lines (core/audit.emit) add it as request_id automatically. Set LOG_REQUEST_ID=true to put it in every
app log line too; the format is unchanged unless you do.
"""
import contextvars
import logging
import os

_current = contextvars.ContextVar("ragleap_request_id", default="")


def bind(request_id: str):
    """Make request_id the current id. Returns a token for unbind."""
    return _current.set(request_id or "")


def unbind(token) -> None:
    try:
        _current.reset(token)
    except Exception:
        _current.set("")


def current() -> str:
    return _current.get()


class RequestIdFilter(logging.Filter):
    """Adds record.request_id (the current id, or "-") so a log format can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current() or "-"
        return True


def log_config() -> dict:
    """Keyword arguments for logging.basicConfig. The line format only changes when LOG_REQUEST_ID is on."""
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    cfg = {"handlers": [handler]}
    if os.environ.get("LOG_REQUEST_ID", "").strip().lower() in ("1", "true", "yes", "on"):
        cfg["format"] = "%(levelname)s:%(name)s:[%(request_id)s] %(message)s"
    return cfg
