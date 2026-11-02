"""Who may open a call on the voice WebSocket server, and the limits on a call.

The server is a raw WebSocket (Twilio Media Streams cannot use the main API), so the API key check does not cover it. A call is
accepted only when the address it connects to ends in the secret VOICE_WS_TOKEN: wss://your-domain.com:8765/<token>. With no
token set (or a token shorter than 16 characters) every connection is refused."""
import hmac
import os
import urllib.parse

MIN_TOKEN_LENGTH = 16
MAX_MESSAGE_BYTES = 65536


def configured_token() -> str:
    value = os.environ.get("VOICE_WS_TOKEN", "").strip()
    return value if len(value) >= MIN_TOKEN_LENGTH else ""


def token_from_path(path) -> str:
    """The one path segment of the connection address, or "" if there is not exactly one."""
    parts = urllib.parse.urlsplit(path or "").path.strip("/").split("/")
    return parts[0] if len(parts) == 1 else ""


def is_allowed(path, expected=None) -> bool:
    expected = configured_token() if expected is None else expected
    if not expected:
        return False
    return hmac.compare_digest(token_from_path(path).encode("utf-8"), expected.encode("utf-8"))


def _int_env(name, default, low, high):
    try:
        return min(high, max(low, int(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def max_calls() -> int:
    return _int_env("VOICE_MAX_CALLS", 10, 1, 100)


def max_call_seconds() -> float:
    return _int_env("VOICE_MAX_CALL_MINUTES", 30, 1, 240) * 60.0
