"""Approval sender check and webhook fail-closed behaviour. No network: settings, senders and signatures mocked."""
import asyncio
import base64
import hashlib
import hmac
import os
import sys
from unittest.mock import patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import autonomy
from core.api import app
from channels.discord import router as dc
from channels.telegram import router as tg
from channels.whatsapp import router as wa


def _cfg(channel="telegram", target="424242"):
    return patch.object(autonomy, "get_autonomy_settings", return_value={
        "mode": "semi", "channels": [], "actions": [], "approval_channel": channel, "approval_target": target})


def _env(**kw):
    clean = {k: v for k, v in os.environ.items() if k not in ("WHATSAPP_ALLOW_UNSIGNED", "TELEGRAM_ALLOW_UNSIGNED")}
    clean.update(kw)
    return patch.dict(os.environ, clean, clear=True)


def test_owner_matches_configured_channel_and_target():
    with _cfg("telegram", "424242"):
        assert autonomy.is_owner_sender("telegram", 424242) is True
        assert autonomy.is_owner_sender("telegram", "424242") is True


def test_whatsapp_number_formats_are_normalised():
    with _cfg("whatsapp", "+91 98765-43210"):
        for sender in ("+919876543210", "919876543210", "whatsapp:+919876543210", "whatsapp:+91 98765 43210"):
            assert autonomy.is_owner_sender("whatsapp", sender) is True, sender
        assert autonomy.is_owner_sender("whatsapp", "+919876543211") is False


def test_wrong_channel_is_not_owner():
    with _cfg("telegram", "424242"):
        assert autonomy.is_owner_sender("discord", "424242") is False
        assert autonomy.is_owner_sender("whatsapp", "424242") is False


def test_wrong_sender_is_not_owner():
    with _cfg("telegram", "424242"):
        for s in (999999, "42424", "4242422", "", None):
            assert autonomy.is_owner_sender("telegram", s) is False, s


def test_unconfigured_target_means_nobody_is_owner():
    with _cfg("telegram", ""):
        assert autonomy.is_owner_sender("telegram", "") is False
        assert autonomy.is_owner_sender("telegram", "anything") is False


def test_owner_check_never_raises_and_fails_closed():
    with patch.object(autonomy, "get_autonomy_settings", side_effect=Exception("db down")):
        assert autonomy.is_owner_sender("telegram", 424242) is False


def test_non_owner_cannot_approve_and_is_told_nothing():
    with _cfg(), patch.object(autonomy, "process_approval_response") as real:
        assert autonomy.process_approval_from("telegram", 999999, "YES ABCD1234") is None
        assert autonomy.process_approval_from("discord", 424242, "NO ABCD1234") is None
    real.assert_not_called()


def test_owner_approval_is_delegated():
    with _cfg(), patch.object(autonomy, "process_approval_response", return_value="done") as real:
        assert autonomy.process_approval_from("telegram", 424242, "YES ABCD1234") == "done"
    real.assert_called_once_with("YES ABCD1234", decided_by={
        "name": "the owner's telegram chat", "role": "owner", "kind": "channel", "user_id": None})


def test_non_owner_approval_attempt_is_logged(caplog):
    with _cfg(), caplog.at_level("WARNING", logger="core.autonomy"):
        autonomy.process_approval_from("telegram", 999999, "yes abcd1234")
        autonomy.process_approval_from("telegram", 999999, "hello there")
    assert caplog.text.count("non-owner sender") == 1


def test_telegram_router_passes_channel_and_sender():
    with patch.object(tg, "process_approval_from", return_value="done") as p, patch.object(tg, "send_telegram_message") as send:
        assert tg.handle_incoming_message(424242, "YES ABCD1234") == "done"
    p.assert_called_once_with("telegram", 424242, "YES ABCD1234")
    send.assert_called_once_with(424242, "done")


def test_whatsapp_router_passes_channel_and_sender():
    with patch.object(wa, "process_approval_from", return_value="done") as p, patch.object(wa, "send_whatsapp_message") as send:
        assert wa.handle_incoming_message("+919876543210", "YES ABCD1234") == "done"
    p.assert_called_once_with("whatsapp", "+919876543210", "YES ABCD1234")
    send.assert_called_once_with("+919876543210", "done")


def test_discord_router_passes_channel_and_sender():
    with patch.object(dc, "process_approval_from", return_value="done") as p, patch.object(dc, "send_discord_message") as send:
        assert dc.handle_incoming_message("555", "YES ABCD1234") == "done"
    p.assert_called_once_with("discord", "555", "YES ABCD1234")
    send.assert_called_once_with("555", "done")


def test_telegram_without_secret_is_rejected():
    with _env(), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", None):
        assert tg._verify_webhook_secret("") is False
        assert tg._verify_webhook_secret("anything") is False


def test_telegram_without_secret_can_be_opted_out_for_local_testing():
    with _env(TELEGRAM_ALLOW_UNSIGNED="true"), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", None):
        assert tg._verify_webhook_secret("") is True


def test_telegram_with_secret_still_compares_strictly():
    with _env(), patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", "s3cret"):
        assert tg._verify_webhook_secret("s3cret") is True
        assert tg._verify_webhook_secret("wrong") is False
        assert tg._verify_webhook_secret("") is False


class _Req:
    def __init__(self, headers):
        self.headers = headers
        self.url = "https://example.com/webhook/whatsapp"
    async def form(self):
        return {"Body": "hello", "From": "whatsapp:+919876543210"}


def _wa_endpoint():
    return next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == "/webhook/whatsapp" and "POST" in getattr(r, "methods", set()))


def _call_wa(headers):
    return asyncio.run(_wa_endpoint()(_Req(headers)))


def test_whatsapp_request_without_signature_is_rejected():
    with _env(), patch.object(wa, "handle_incoming_message") as h:
        with pytest.raises(HTTPException) as exc:
            _call_wa({})
    assert exc.value.status_code == 403
    h.assert_not_called()


def test_whatsapp_unsigned_allowed_only_with_explicit_opt_out():
    with _env(WHATSAPP_ALLOW_UNSIGNED="true"), patch.object(wa, "handle_incoming_message") as h:
        _call_wa({})
    h.assert_called_once_with("+919876543210", "hello")


def test_whatsapp_invalid_signature_is_rejected_and_valid_is_accepted():
    with _env(WHATSAPP_ALLOW_UNSIGNED="true"), patch.object(wa, "handle_incoming_message") as h, \
         patch.object(wa, "_verify_twilio_signature", return_value=False):
        with pytest.raises(HTTPException) as exc:
            _call_wa({"x-twilio-signature": "bad"})
    assert exc.value.status_code == 403
    h.assert_not_called()
    with _env(), patch.object(wa, "handle_incoming_message") as h, \
         patch.object(wa, "_verify_twilio_signature", return_value=True):
        _call_wa({"x-twilio-signature": "good"})
    h.assert_called_once()


# Credentials carrying one character a str compare_digest refuses: a latin-1
# byte, a non-latin-1 BMP character, an astral (emoji) character, a lone surrogate.
NON_ASCII_CREDENTIALS = [
    pytest.param("\xff", id="latin1"),
    pytest.param("中", id="bmp"),
    pytest.param("\U0001f600", id="astral"),
    pytest.param("\udcff", id="surrogate"),
]


def _twilio_signature(token: str, url: str, params: dict) -> str:
    """The documented Twilio signing: HMAC-SHA1 over url + sorted key+value, base64."""
    full_url = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode("utf-8"), full_url.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")


def test_whatsapp_valid_twilio_signature_is_processed():
    """A correctly computed signature verifies through the real verifier and the handler runs."""
    token = "example-twilio-auth-token"
    params = {"Body": "hello", "From": "whatsapp:+919876543210"}
    signature = _twilio_signature(token, _Req({}).url, params)
    with _env(), patch.object(wa, "TWILIO_AUTH_TOKEN", token), \
         patch.object(wa, "handle_incoming_message") as h:
        _call_wa({"x-twilio-signature": signature})
    h.assert_called_once_with("+919876543210", "hello")


def test_twilio_signature_verifier_accepts_only_the_documented_signature():
    """The right signature verifies; wrong, empty and missing signatures and an unset token all fail closed."""
    token = "example-twilio-auth-token"
    url = _Req({}).url
    params = {"Body": "hello", "From": "whatsapp:+919876543210"}
    signature = _twilio_signature(token, url, params)
    with patch.object(wa, "TWILIO_AUTH_TOKEN", token):
        assert wa._verify_twilio_signature(url, params, signature) is True
        assert wa._verify_twilio_signature(url, params, signature[:-1] + "A") is False
        assert wa._verify_twilio_signature(url, params, "") is False
    with patch.object(wa, "TWILIO_AUTH_TOKEN", None):
        assert wa._verify_twilio_signature(url, params, signature) is False


@pytest.mark.parametrize("side", ["received", "configured"])
@pytest.mark.parametrize("credential", NON_ASCII_CREDENTIALS)
def test_twilio_signature_verifier_never_raises_on_non_ascii_credentials(credential, side):
    """Any non-ASCII character on either side of the signature comparison is a failed verification (False -> 403), never a raised error."""
    url = _Req({}).url
    params = {"Body": "hello", "From": "whatsapp:+919876543210"}
    if side == "received":
        token, signature = "example-twilio-auth-token", "AAAA" + credential
    else:
        token, signature = "example-twilio-auth-token" + credential, "AAAA"
    with patch.object(wa, "TWILIO_AUTH_TOKEN", token):
        try:
            result = wa._verify_twilio_signature(url, params, signature)
        except Exception as exc:
            pytest.fail(f"verifier raised {type(exc).__name__}: {exc}")
    assert result is False


def test_whatsapp_non_ascii_twilio_signature_is_rejected_before_processing():
    """A non-ASCII X-Twilio-Signature is an invalid credential, not a server error: the real verifier fails it 403 and the handler never runs."""
    with _env(), patch.object(wa, "TWILIO_AUTH_TOKEN", "example-twilio-auth-token"), \
         patch.object(wa, "handle_incoming_message") as h:
        with pytest.raises(Exception) as excinfo:
            _call_wa({"x-twilio-signature": "\udcff"})
    assert isinstance(excinfo.value, HTTPException)
    assert excinfo.value.status_code == 403
    h.assert_not_called()
