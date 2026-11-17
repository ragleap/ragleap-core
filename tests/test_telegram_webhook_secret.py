"""Telegram webhook secret check: non-ASCII secrets never raise and compare exactly, as UTF-8 bytes on both sides."""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from channels.telegram import router as tg
from core.api import app


def _env(**kw):
    """Patch os.environ with TELEGRAM_ALLOW_UNSIGNED cleared unless a test sets it."""
    clean = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_ALLOW_UNSIGNED",)}
    clean.update(kw)
    return patch.dict(os.environ, clean, clear=True)


# (label, value) pairs; failure messages carry the label only, never a secret-shaped value.
# The classes past plain ASCII differ from an existing member only through a
# normalisation a non-injective compare could hide (casefold, strip, NFKC composition,
# surrogateescape byte-recovery), so the cross-product rejects each of them.
_SECRET_CASES = (
    ("ascii", "example-secret"),
    ("ascii-upper", "example-SECRET"),
    ("wrapped", " example-secret "),
    ("latin1-y", "example-secreÿ"),
    ("latin1-e", "example-é"),
    ("latin1-e-decomposed", "example-é"),
    ("non-bmp", "example-\U0001f600"),
    ("lone-surrogate", "example-secret\udcff"),
    ("surrogate-escaped", "example-secre\udcc3\udcbf"),
)


class _Req:
    """Request double carrying headers and a JSON body, the only surfaces the endpoint reads."""

    def __init__(self, headers, body=None):
        self.headers = headers
        self._body = body if body is not None else {
            "message": {"chat": {"id": 424242, "type": "private"}, "from": {"id": 424242}, "text": "example-text"},
        }

    async def json(self):
        return self._body


def _tg_endpoint():
    """Return the /webhook/telegram endpoint function from the app's routes."""
    return next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/webhook/telegram")


def _call_tg(headers, body=None):
    """Drive the /webhook/telegram endpoint with the given headers and body."""
    return asyncio.run(_tg_endpoint()(_Req(headers, body)))


def _call_tg_expecting_403(headers):
    """Call the endpoint expecting HTTPException 403; fail the assertion on TypeError or on acceptance."""
    try:
        _call_tg(headers)
        pytest.fail("endpoint accepted a request it must reject with 403")
    except TypeError as exc:
        pytest.fail(f"endpoint raised {type(exc).__name__} instead of HTTPException(403)")
    except HTTPException as exc:
        assert exc.status_code == 403


def _verify_or_fail(received, configured, rlabel, clabel):
    """Call _verify_webhook_secret with the secret patched, turning any exception into an assertion failure."""
    with patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", configured):
        try:
            return tg._verify_webhook_secret(received)
        except Exception as exc:
            pytest.fail(
                f"_verify_webhook_secret(received={rlabel}, configured={clabel}) "
                f"raised {type(exc).__name__} instead of returning a verdict"
            )


def test_verify_never_raises_and_returns_str_equality_across_all_encodings():
    """For every received/configured pair across ASCII, upper-case, whitespace-wrapped, precomposed and decomposed Latin-1, non-BMP and lone-surrogate (surrogatepass and surrogateescape forms) secrets the check returns received == configured and never raises."""
    for rlabel, received in _SECRET_CASES:
        for clabel, configured in _SECRET_CASES:
            result = _verify_or_fail(received, configured, rlabel, clabel)
            assert result is (received == configured), (rlabel, clabel)


def test_endpoint_rejects_non_ascii_header_with_403_not_500():
    """A header carrying a character above U+007F is rejected with HTTPException 403, never a TypeError, and the handler is not called."""
    with _env(), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", "example-secret"), \
         patch.object(tg, "handle_incoming_message") as handler:
        _call_tg_expecting_403({"x-telegram-bot-api-secret-token": "example-secreÿ"})
        handler.assert_not_called()


def test_endpoint_rejects_non_ascii_configured_secret_with_403_not_500():
    """A non-ASCII configured secret (Latin-1 or a lone surrogate) rejects an ASCII header with 403 and no encode error."""
    for configured in ("example-secreÿ", "example-secret\udcff"):
        with _env(), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", configured), \
             patch.object(tg, "handle_incoming_message") as handler:
            _call_tg_expecting_403({"x-telegram-bot-api-secret-token": "example-secret"})
            handler.assert_not_called()


def test_fail_closed_controls_are_unchanged():
    """The exact valid secret reaches the handler once; wrong, empty and unconfigured secrets reject with 403; TELEGRAM_ALLOW_UNSIGNED=true with no secret still accepts."""
    with _env(), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", "example-secret"), \
         patch.object(tg, "handle_incoming_message") as handler:
        _call_tg({"x-telegram-bot-api-secret-token": "example-secret"})
    handler.assert_called_once_with(424242, "example-text")

    with _env(), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", "example-secret"), \
         patch.object(tg, "handle_incoming_message") as handler:
        _call_tg_expecting_403({"x-telegram-bot-api-secret-token": "example-other"})
        handler.assert_not_called()

    with _env(), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", "example-secret"), \
         patch.object(tg, "handle_incoming_message") as handler:
        _call_tg_expecting_403({})
        handler.assert_not_called()

    with _env(), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", None), \
         patch.object(tg, "handle_incoming_message") as handler:
        _call_tg_expecting_403({"x-telegram-bot-api-secret-token": "example-secret"})
        handler.assert_not_called()

    with _env(TELEGRAM_ALLOW_UNSIGNED="true"), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", None), \
         patch.object(tg, "handle_incoming_message") as handler:
        _call_tg({})
    handler.assert_called_once_with(424242, "example-text")


def test_lossy_encoding_cannot_smuggle_a_match():
    """A header differing from the configured secret only where a lossy encoding would drop or replace a character is rejected with 403."""
    with _env(), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", "example-secret\udcff"), \
         patch.object(tg, "handle_incoming_message") as handler:
        _call_tg_expecting_403({"x-telegram-bot-api-secret-token": "example-secret"})
        _call_tg_expecting_403({"x-telegram-bot-api-secret-token": "example-secret?"})
        handler.assert_not_called()
